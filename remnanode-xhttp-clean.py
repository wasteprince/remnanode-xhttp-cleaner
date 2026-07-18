#!/usr/bin/env python3
"""Remove stale outbound TCP sockets owned by RemnaNode's rw-core process."""

from __future__ import annotations

import argparse
import dataclasses
import errno
import ipaddress
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


VERSION = "2.1.0"
PROGRAM = "remnanode-xhttp-clean"
AUTHOR = "Bankaev"
CONFIG_PATH = Path(os.environ.get("XHTTP_CLEAN_CONFIG", "/etc/remnanode-xhttp-clean.json"))
INSTALL_PATH = Path("/usr/local/sbin/remnanode-xhttp-clean")
CONTROL_PATH = Path("/usr/local/bin/xhttp-cleaner")
SERVICE_PATH = Path("/etc/systemd/system/remnanode-xhttp-clean.service")
TIMER_PATH = Path("/etc/systemd/system/remnanode-xhttp-clean.timer")

NETLINK_SOCK_DIAG = 4
SOCK_DIAG_BY_FAMILY = 20
SOCK_DESTROY = 21
NLMSG_ERROR = 2
NLMSG_DONE = 3
NLM_F_REQUEST = 0x01
NLM_F_ACK = 0x04
NLM_F_ROOT = 0x100
NLM_F_MATCH = 0x200
NLM_F_DUMP = NLM_F_ROOT | NLM_F_MATCH
INET_DIAG_INFO = 2
INET_DIAG_NOCOOKIE = 0xFFFFFFFF

TCP_ESTABLISHED = 1
TCP_CLOSE_WAIT = 8
TCP_LISTEN = 10
STATE_NAMES = {
    TCP_ESTABLISHED: "ESTABLISHED",
    TCP_CLOSE_WAIT: "CLOSE-WAIT",
    TCP_LISTEN: "LISTEN",
}
TARGET_STATES = (TCP_ESTABLISHED, TCP_CLOSE_WAIT)


class CleanerError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class Config:
    container: str = "remnanode"
    idle_seconds: int = 300
    include_inbound: bool = False
    exclude_loopback: bool = True

    @classmethod
    def load(cls) -> "Config":
        if not CONFIG_PATH.exists():
            return cls()
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CleanerError(f"Не удалось прочитать {CONFIG_PATH}: {exc}") from exc
        if not isinstance(raw, dict):
            raise CleanerError("Конфигурация должна быть JSON-объектом")
        allowed = {field.name for field in dataclasses.fields(cls)}
        unknown = set(raw) - allowed
        if unknown:
            raise CleanerError(f"Неизвестные параметры: {', '.join(sorted(unknown))}")
        config = cls(**raw)
        config.validate()
        return config

    def validate(self) -> None:
        if not isinstance(self.container, str) or not self.container:
            raise CleanerError("container должен быть непустой строкой")
        if not isinstance(self.idle_seconds, int) or isinstance(self.idle_seconds, bool):
            raise CleanerError("idle_seconds должен быть целым числом")
        if self.idle_seconds < 300:
            raise CleanerError("idle_seconds нельзя устанавливать меньше 300 секунд")
        if not isinstance(self.include_inbound, bool) or not isinstance(self.exclude_loopback, bool):
            raise CleanerError("include_inbound и exclude_loopback должны быть boolean")


@dataclasses.dataclass(frozen=True)
class SocketRecord:
    family: int
    state: int
    local_address: str
    local_port: int
    remote_address: str
    remote_port: int
    interface: int
    cookie: Tuple[int, int]
    inode: int
    recv_queue: int
    send_queue: int
    last_sent_ms: Optional[int]
    last_received_ms: Optional[int]
    sockid: bytes = dataclasses.field(repr=False)

    @property
    def idle_ms(self) -> Optional[int]:
        # A connection is inactive only when neither direction transferred data.
        if not self.last_sent_ms or not self.last_received_ms:
            return None
        return min(self.last_sent_ms, self.last_received_ms)

    @property
    def identity(self) -> Tuple[int, Tuple[int, int]]:
        # The kernel cookie is the reuse-safe identity; inode is an ownership guard.
        return self.inode, self.cookie

    def endpoint(self, address: str, port: int) -> str:
        if self.family == socket.AF_INET6:
            return f"[{address}]:{port}"
        return f"{address}:{port}"

    @property
    def local(self) -> str:
        return self.endpoint(self.local_address, self.local_port)

    @property
    def remote(self) -> str:
        return self.endpoint(self.remote_address, self.remote_port)


def align4(length: int) -> int:
    return (length + 3) & ~3


def state_mask(states: Iterable[int]) -> int:
    result = 0
    for state in states:
        result |= 1 << state
    return result


class DiagClient:
    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_SOCK_DIAG)
        self.sock.settimeout(3.0)
        self.sequence = int(time.time()) & 0x7FFFFFFF

    def close(self) -> None:
        self.sock.close()

    def __enter__(self) -> "DiagClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _send(self, message_type: int, flags: int, payload: bytes) -> int:
        self.sequence += 1
        header = struct.pack(
            "=IHHII", 16 + len(payload), message_type, flags, self.sequence, os.getpid()
        )
        self.sock.send(header + payload)
        return self.sequence

    @staticmethod
    def _request_payload(
        family: int, states: int, sockid: Optional[bytes] = None, request_info: bool = True
    ) -> bytes:
        extensions = 1 << (INET_DIAG_INFO - 1) if request_info else 0
        if sockid is None:
            sockid = b"\0" * 40 + struct.pack("=II", INET_DIAG_NOCOOKIE, INET_DIAG_NOCOOKIE)
        if len(sockid) != 48:
            raise CleanerError("Некорректная длина inet_diag_sockid")
        return struct.pack("=BBBBI", family, socket.IPPROTO_TCP, extensions, 0, states) + sockid

    def dump(self, states: Sequence[int], family: int) -> List[SocketRecord]:
        payload = self._request_payload(family, state_mask(states))
        sequence = self._send(SOCK_DIAG_BY_FAMILY, NLM_F_REQUEST | NLM_F_DUMP, payload)
        return self._receive_records(sequence, expect_done=True)

    def query_exact(self, record: SocketRecord) -> Optional[SocketRecord]:
        payload = self._request_payload(
            record.family, state_mask((record.state,)), record.sockid, request_info=True
        )
        sequence = self._send(SOCK_DIAG_BY_FAMILY, NLM_F_REQUEST, payload)
        records = self._receive_records(sequence, expect_done=False)
        return records[0] if records else None

    def destroy(self, record: SocketRecord) -> bool:
        # SOCK_DESTROY receives the exact sockid, including the kernel cookie.
        # If the old tuple was reused, the cookie mismatch makes the request fail
        # instead of closing the new socket.
        payload = self._request_payload(
            record.family, 0, record.sockid, request_info=False
        )
        sequence = self._send(SOCK_DESTROY, NLM_F_REQUEST | NLM_F_ACK, payload)
        while True:
            data = self.sock.recv(65535)
            offset = 0
            while offset + 16 <= len(data):
                length, message_type, _flags, msg_sequence, _pid = struct.unpack_from(
                    "=IHHII", data, offset
                )
                if length < 16:
                    raise CleanerError("Повреждённый netlink-ответ")
                payload_view = data[offset + 16 : offset + length]
                offset += align4(length)
                if msg_sequence != sequence:
                    continue
                if message_type != NLMSG_ERROR or len(payload_view) < 4:
                    continue
                error_code = struct.unpack_from("=i", payload_view, 0)[0]
                if error_code == 0:
                    return True
                if -error_code in (errno.ENOENT, errno.ESRCH):
                    return False
                raise OSError(-error_code, os.strerror(-error_code))

    def _receive_records(self, sequence: int, expect_done: bool) -> List[SocketRecord]:
        records: List[SocketRecord] = []
        while True:
            try:
                data = self.sock.recv(1 << 20)
            except socket.timeout:
                if not expect_done and not records:
                    return []
                raise CleanerError("Тайм-аут ответа NETLINK_SOCK_DIAG")
            offset = 0
            while offset + 16 <= len(data):
                length, message_type, _flags, msg_sequence, _pid = struct.unpack_from(
                    "=IHHII", data, offset
                )
                if length < 16:
                    raise CleanerError("Повреждённый netlink-ответ")
                payload = data[offset + 16 : offset + length]
                offset += align4(length)
                if msg_sequence != sequence:
                    continue
                if message_type == NLMSG_DONE:
                    return records
                if message_type == NLMSG_ERROR:
                    if len(payload) < 4:
                        raise CleanerError("Короткий NLMSG_ERROR")
                    error_code = struct.unpack_from("=i", payload, 0)[0]
                    if error_code in (0, -errno.ENOENT, -errno.ESRCH):
                        return records
                    raise OSError(-error_code, os.strerror(-error_code))
                record = parse_diag_record(payload)
                if record is not None:
                    records.append(record)
                if not expect_done:
                    return records


def parse_diag_record(payload: bytes) -> Optional[SocketRecord]:
    if len(payload) < 72:
        return None
    family, state, _timer, _retrans = struct.unpack_from("=BBBB", payload, 0)
    if family not in (socket.AF_INET, socket.AF_INET6):
        return None
    sockid = payload[4:52]
    local_port, remote_port = struct.unpack_from("!HH", sockid, 0)
    address_size = 4 if family == socket.AF_INET else 16
    local_raw = sockid[4 : 4 + address_size]
    remote_raw = sockid[20 : 20 + address_size]
    local_address = socket.inet_ntop(family, local_raw)
    remote_address = socket.inet_ntop(family, remote_raw)
    interface = struct.unpack_from("=I", sockid, 36)[0]
    cookie = struct.unpack_from("=II", sockid, 40)
    _expires, recv_queue, send_queue, _uid, inode = struct.unpack_from("=IIIII", payload, 52)
    last_sent_ms: Optional[int] = None
    last_received_ms: Optional[int] = None

    offset = 72
    while offset + 4 <= len(payload):
        attribute_length, attribute_type = struct.unpack_from("=HH", payload, offset)
        if attribute_length < 4 or offset + attribute_length > len(payload):
            break
        value = payload[offset + 4 : offset + attribute_length]
        if attribute_type == INET_DIAG_INFO and len(value) >= 56:
            last_sent_ms = struct.unpack_from("=I", value, 44)[0]
            last_received_ms = struct.unpack_from("=I", value, 52)[0]
        offset += align4(attribute_length)

    return SocketRecord(
        family=family,
        state=state,
        local_address=local_address,
        local_port=local_port,
        remote_address=remote_address,
        remote_port=remote_port,
        interface=interface,
        cookie=(cookie[0], cookie[1]),
        inode=inode,
        recv_queue=recv_queue,
        send_queue=send_queue,
        last_sent_ms=last_sent_ms,
        last_received_ms=last_received_ms,
        sockid=sockid,
    )


def owned_socket_inodes(pid: int) -> Set[int]:
    result: Set[int] = set()
    fd_path = Path(f"/proc/{pid}/fd")
    try:
        entries = list(fd_path.iterdir())
    except OSError as exc:
        raise CleanerError(f"Не удалось прочитать {fd_path}: {exc}") from exc
    for entry in entries:
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        if target.startswith("socket:[") and target.endswith("]"):
            try:
                result.add(int(target[8:-1]))
            except ValueError:
                continue
    return result


def is_loopback(address: str) -> bool:
    try:
        return ipaddress.ip_address(address.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def candidate_reason(
    record: SocketRecord,
    owned_inodes: Set[int],
    listen_ports: Set[int],
    config: Config,
) -> Optional[str]:
    if record.state not in TARGET_STATES or record.inode not in owned_inodes:
        return None
    if not config.include_inbound and record.local_port in listen_ports:
        return None
    if config.exclude_loopback and (
        is_loopback(record.local_address) or is_loopback(record.remote_address)
    ):
        return None
    idle_ms = record.idle_ms
    if idle_ms is None or idle_ms < config.idle_seconds * 1000:
        return None
    return f"нет передачи данных {idle_ms // 1000} с"


def dump_all(client: DiagClient) -> List[SocketRecord]:
    states = (*TARGET_STATES, TCP_LISTEN)
    records: List[SocketRecord] = []
    for family in (socket.AF_INET, socket.AF_INET6):
        records.extend(client.dump(states, family))
    return records


def worker(pid: int, config: Config, apply: bool) -> Dict[str, object]:
    owned_before = owned_socket_inodes(pid)
    with DiagClient() as client:
        records = dump_all(client)
        listen_ports = {
            item.local_port
            for item in records
            if item.state == TCP_LISTEN and item.inode in owned_before
        }
        candidates = [
            item
            for item in records
            if candidate_reason(item, owned_before, listen_ports, config) is not None
        ]
        candidates.sort(key=lambda item: item.idle_ms or 0, reverse=True)

        closed: List[SocketRecord] = []
        skipped_changed = 0
        if apply:
            # Refresh ownership once after the initial dump. Re-reading tens of
            # thousands of fd symlinks for every candidate would be O(n²).
            # Per-socket tuple reuse is still guarded by query_exact + cookie.
            owned_now = owned_socket_inodes(pid)
            for original in candidates:
                # Re-query by the original kernel cookie immediately before destroy.
                # A newly-created socket may have the same IP/ports/inode, but never
                # the same cookie, so it cannot pass this check or be destroyed.
                current = client.query_exact(original)
                if current is None or current.identity != original.identity:
                    skipped_changed += 1
                    continue
                if candidate_reason(current, owned_now, listen_ports, config) is None:
                    skipped_changed += 1
                    continue
                if client.destroy(current):
                    closed.append(current)
                else:
                    skipped_changed += 1

    return {
        "pid": pid,
        "owned_sockets": len(owned_before),
        "listen_ports": sorted(listen_ports),
        "candidates": [record_to_dict(item) for item in candidates],
        "closed": [record_to_dict(item) for item in closed],
        "skipped_changed": skipped_changed,
    }


def record_to_dict(record: SocketRecord) -> Dict[str, object]:
    return {
        "state": STATE_NAMES.get(record.state, str(record.state)),
        "local": record.local,
        "remote": record.remote,
        "idle_seconds": (record.idle_ms or 0) // 1000,
        "inode": record.inode,
        "cookie": f"{record.cookie[0]:08x}:{record.cookie[1]:08x}",
        "recv_queue": record.recv_queue,
        "send_queue": record.send_queue,
    }


def run(command: Sequence[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, text=True, capture_output=True, check=check)
    except FileNotFoundError as exc:
        raise CleanerError(f"Команда не найдена: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or f"код {exc.returncode}"
        raise CleanerError(f"Ошибка {' '.join(command)}: {detail}") from exc


def require_root() -> None:
    if os.geteuid() != 0:
        raise CleanerError("Для чтения чужих socket inode и SOCK_DESTROY нужен root")


def docker_info(config: Config) -> Tuple[int, int, str]:
    if shutil.which("docker") is None:
        raise CleanerError("Docker CLI не найден")
    running = run(["docker", "inspect", "-f", "{{.State.Running}}", config.container])
    if running.stdout.strip() != "true":
        raise CleanerError(f"Контейнер {config.container!r} не запущен")
    init_pid_text = run(["docker", "inspect", "-f", "{{.State.Pid}}", config.container])
    try:
        init_pid = int(init_pid_text.stdout.strip())
    except ValueError as exc:
        raise CleanerError("Docker вернул некорректный PID контейнера") from exc

    top = run(["docker", "top", config.container, "-eo", "pid,comm,args"])
    xray_pid: Optional[int] = None
    for line in top.stdout.splitlines()[1:]:
        fields = line.split(None, 2)
        if len(fields) < 2:
            continue
        command_name = fields[1]
        arguments = fields[2] if len(fields) > 2 else ""
        if command_name in ("rw-core", "xray") or "/rw-core" in arguments:
            xray_pid = int(fields[0])
            break
    if xray_pid is None:
        raise CleanerError("Процесс rw-core/xray внутри контейнера не найден")
    image = run(["docker", "inspect", "-f", "{{.Config.Image}}", config.container]).stdout.strip()
    return init_pid, xray_pid, image


def run_worker(config: Config, apply: bool) -> Dict[str, object]:
    require_root()
    if shutil.which("nsenter") is None:
        raise CleanerError("nsenter не найден; установите пакет util-linux")
    init_pid, xray_pid, _image = docker_info(config)
    command = [
        "nsenter",
        "--target",
        str(init_pid),
        "--net",
        "--",
        sys.executable,
        str(Path(__file__).resolve()),
        "_worker",
        "--pid",
        str(xray_pid),
        "--config-json",
        json.dumps(dataclasses.asdict(config), separators=(",", ":")),
    ]
    if apply:
        command.append("--apply")
    completed = run(command)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise CleanerError(f"Некорректный ответ worker: {completed.stdout!r}") from exc


def process_rss_mb(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError):
        pass
    return 0


def print_records(records: Sequence[Dict[str, object]]) -> None:
    if not records:
        print("Подходящих неактивных исходящих сокетов нет.")
        return
    print("STATE        IDLE       LOCAL                         REMOTE                        INODE       COOKIE")
    for item in records:
        print(
            f"{str(item['state']):<12} "
            f"{str(item['idle_seconds']) + 's':<10} "
            f"{str(item['local']):<29} "
            f"{str(item['remote']):<29} "
            f"{str(item['inode']):<11} "
            f"{item['cookie']}"
        )


def command_status(config: Config) -> None:
    _init_pid, xray_pid, image = docker_info(config)
    result = run_worker(config, apply=False)
    print(f"container={config.container}")
    print(f"image={image}")
    print(f"xray_pid={xray_pid}")
    print(f"xray_rss_mb={process_rss_mb(xray_pid)}")
    print(f"owned_tcp_sockets={result['owned_sockets']}")
    print(f"stale_outbound_sockets={len(result['candidates'])}")
    print(f"idle_seconds={config.idle_seconds}")
    print(f"listening_ports={','.join(map(str, result['listen_ports']))}")


def command_scan(config: Config) -> None:
    result = run_worker(config, apply=False)
    print_records(result["candidates"])  # type: ignore[arg-type]


def command_clean(config: Config, dry_run: bool) -> None:
    result = run_worker(config, apply=not dry_run)
    candidates = result["candidates"]
    closed = result["closed"]
    if dry_run:
        print_records(candidates)  # type: ignore[arg-type]
        print(f"Dry-run: найдено {len(candidates)}, ничего не закрыто.")
        return
    print(f"Найдено перед повторной проверкой: {len(candidates)}")
    print(f"Закрыто по inode + kernel cookie: {len(closed)}")
    print(f"Пропущено как изменившиеся/активные: {result['skipped_changed']}")
    print_records(closed)  # type: ignore[arg-type]


def atomic_write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def command_install(config: Config) -> None:
    require_root()
    docker_info(config)
    if shutil.which("systemctl") is None:
        raise CleanerError("systemd не найден")
    if shutil.which("nsenter") is None:
        raise CleanerError("nsenter не найден; установите util-linux")
    source_path = Path(__file__).resolve()
    installed_path = INSTALL_PATH.resolve() if INSTALL_PATH.exists() else INSTALL_PATH
    if source_path != installed_path:
        shutil.copy2(source_path, INSTALL_PATH)
        os.chmod(INSTALL_PATH, 0o755)
    if not CONFIG_PATH.exists():
        atomic_write(
            CONFIG_PATH,
            json.dumps(dataclasses.asdict(config), ensure_ascii=False, indent=2) + "\n",
            0o640,
        )
    atomic_write(
        SERVICE_PATH,
        """[Unit]
Description=XHTTP Cleaner by Bankaev - clean stale outbound rw-core sockets
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/remnanode-xhttp-clean clean
Nice=10
""",
        0o644,
    )
    atomic_write(
        TIMER_PATH,
        """[Unit]
Description=XHTTP Cleaner by Bankaev - run every five minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=5min
RandomizedDelaySec=20s
Persistent=true
Unit=remnanode-xhttp-clean.service

[Install]
WantedBy=timers.target
""",
        0o644,
    )
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", "remnanode-xhttp-clean.timer"])
    print(f"Установлено. Сокеты без данных >= {config.idle_seconds} с будут очищаться каждые 5 минут.")


def command_uninstall() -> None:
    require_root()
    if shutil.which("systemctl"):
        run(["systemctl", "disable", "--now", "remnanode-xhttp-clean.timer"], check=False)
    for path in (SERVICE_PATH, TIMER_PATH, CONFIG_PATH, INSTALL_PATH, CONTROL_PATH):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    if shutil.which("systemctl"):
        run(["systemctl", "daemon-reload"], check=False)
    print("Timer, service, конфигурация и установленный скрипт удалены.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Очистка исходящих TCP-сокетов rw-core без активности не менее 5 минут.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION} by {AUTHOR}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="Краткая статистика без изменений")
    subparsers.add_parser("scan", help="Показать кандидатов без закрытия")
    clean_parser = subparsers.add_parser("clean", help="Повторно проверить и закрыть старые сокеты")
    clean_parser.add_argument("--dry-run", action="store_true", help="Только показать кандидатов")
    subparsers.add_parser("install", help="Установить systemd timer")
    subparsers.add_parser("uninstall", help="Удалить systemd timer и скрипт")

    worker_parser = subparsers.add_parser("_worker", help=argparse.SUPPRESS)
    worker_parser.add_argument("--pid", type=int, required=True)
    worker_parser.add_argument("--config-json", required=True)
    worker_parser.add_argument("--apply", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "_worker":
            raw = json.loads(args.config_json)
            config = Config(**raw)
            config.validate()
            print(json.dumps(worker(args.pid, config, args.apply), separators=(",", ":")))
            return 0

        if args.command == "uninstall":
            command_uninstall()
            return 0

        config = Config.load()
        if args.command == "status":
            require_root()
            command_status(config)
        elif args.command == "scan":
            command_scan(config)
        elif args.command == "clean":
            command_clean(config, args.dry_run)
        elif args.command == "install":
            command_install(config)
        return 0
    except (CleanerError, OSError, json.JSONDecodeError) as exc:
        print(f"{PROGRAM}: ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
