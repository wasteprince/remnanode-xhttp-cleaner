#!/usr/bin/env python3
"""Build, deploy and maintain the version-matched Xray XHTTP Cleaner fork."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Sequence


VERSION = "3.0.0"
PATCH_ID = "xhttp-cleaner-v3"
DEFAULT_CONFIG = Path("/etc/remnanode-xhttp-clean.json")
STATE_ROOT = Path("/var/lib/remnanode-xhttp-clean")
CACHE_ROOT = Path("/var/cache/remnanode-xhttp-clean")
INSTALLED_ASSETS = Path("/usr/local/lib/remnanode-xhttp-clean/xray_patch")
XRAY_REPOSITORY = "https://github.com/XTLS/Xray-core.git"
CORE_CANDIDATES = ("/usr/local/bin/xray", "/usr/local/bin/rw-core")


class CoreManagerError(RuntimeError):
    pass


def run(
    command: Sequence[str], *, check: bool = True, capture: bool = True, timeout: int | None = None
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(command),
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        timeout=timeout,
    )
    if check and completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise CoreManagerError(f"command failed ({completed.returncode}): {' '.join(command)}: {stderr}")
    return completed


def docker(container: str, *arguments: str, check: bool = True, timeout: int | None = None) -> str:
    completed = run(["docker", *arguments], check=check, timeout=timeout)
    return (completed.stdout or "").strip()


def load_container(config_path: Path = DEFAULT_CONFIG) -> str:
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CoreManagerError(f"cannot read {config_path}: {exc}") from exc
    container = raw.get("container", "remnanode")
    if not isinstance(container, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", container):
        raise CoreManagerError("invalid Docker container name in configuration")
    return container


def require_environment(container: str) -> None:
    if os.geteuid() != 0:
        raise CoreManagerError("run this command as root")
    for executable in ("docker",):
        if shutil.which(executable) is None:
            raise CoreManagerError(f"required executable is missing: {executable}")
    if docker(container, "inspect", "-f", "{{.State.Running}}", container) != "true":
        raise CoreManagerError(f"container is not running: {container}")


def core_path(container: str) -> str:
    for candidate in CORE_CANDIDATES:
        resolved = docker(
            container,
            "exec",
            container,
            "readlink",
            "-f",
            candidate,
            check=False,
        )
        if resolved.startswith("/") and re.fullmatch(r"/[A-Za-z0-9_./+-]+", resolved):
            # Verify the resolved path by executing it; no shell is involved.
            probe = run(["docker", "exec", container, resolved, "version"], check=False)
            if probe.returncode == 0:
                return resolved
    raise CoreManagerError("cannot locate an executable xray/rw-core inside the container")


def version_statement(container: str, binary: str) -> str:
    return docker(container, "exec", container, binary, "version")


def parse_version(statement: str) -> str:
    match = re.search(r"(?m)^Xray\s+(\d+\.\d+\.\d+)\b", statement)
    if not match:
        raise CoreManagerError(f"cannot parse Xray version from: {statement.splitlines()[:1]}")
    return match.group(1)


def normalize_arch(raw: str) -> tuple[str, str]:
    mapping = {
        "x86_64": ("amd64", "amd64"),
        "amd64": ("amd64", "amd64"),
        "aarch64": ("arm64", "arm64"),
        "arm64": ("arm64", "arm64"),
    }
    try:
        return mapping[raw.strip()]
    except KeyError as exc:
        raise CoreManagerError(f"unsupported container architecture: {raw!r}") from exc


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    except BaseException:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass
        raise


@contextlib.contextmanager
def operation_lock():
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    lock_path = STATE_ROOT / "core-manager.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def preserved_container_settings(inspect_payload: str) -> dict[str, Any]:
    """Return only immutable/user-configured settings, excluding runtime state."""
    try:
        item = json.loads(inspect_payload)[0]
    except (json.JSONDecodeError, IndexError, KeyError, TypeError) as exc:
        raise CoreManagerError("Docker returned malformed container inspection data") from exc
    networks = item.get("NetworkSettings", {}).get("Networks", {})
    stable_networks = {
        name: {
            key: values.get(key)
            for key in ("Aliases", "Links", "IPAMConfig", "DriverOpts", "NetworkID")
        }
        for name, values in networks.items()
    }
    return {
        "Config": item.get("Config"),
        "HostConfig": item.get("HostConfig"),
        "Mounts": item.get("Mounts"),
        "Networks": stable_networks,
    }


def installed_assets() -> Path:
    local = Path(__file__).resolve().parent / "xray_patch"
    assets = local if local.is_dir() else INSTALLED_ASSETS
    required = (assets / "patch_xray.py", assets / "xhttp_cleaner_reaper.go")
    if not all(path.is_file() for path in required):
        raise CoreManagerError(f"Xray patch assets are incomplete: {assets}")
    return assets


def current_info(container: str) -> dict[str, str]:
    binary = core_path(container)
    statement = version_statement(container, binary)
    goarch, arch_key = normalize_arch(docker(container, "exec", container, "uname", "-m"))
    return {
        "binary": binary,
        "statement": statement,
        "version": parse_version(statement),
        "goarch": goarch,
        "arch": arch_key,
        "patched": str(PATCH_ID in statement).lower(),
        "container_id": docker(container, "inspect", "-f", "{{.Id}}", container),
    }


def version_dir(info: dict[str, str]) -> Path:
    return STATE_ROOT / "versions" / f"{info['version']}-{info['arch']}"


def artifact_path(info: dict[str, str]) -> Path:
    return CACHE_ROOT / "artifacts" / PATCH_ID / f"{info['version']}-{info['arch']}" / "xray"


def clone_and_build(info: dict[str, str], destination: Path) -> None:
    assets = installed_assets()
    tag = f"v{info['version']}"
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise CoreManagerError(f"unsafe source tag: {tag}")
    with tempfile.TemporaryDirectory(prefix="xhttp-cleaner-build-") as temporary:
        workspace = Path(temporary)
        source = workspace / "xray-core"
        output = workspace / "output"
        output.mkdir()
        print(f"core-build: cloning exact upstream tag {tag}", flush=True)
        run(
            ["git", "clone", "--quiet", "--depth", "1", "--branch", tag, XRAY_REPOSITORY, str(source)],
            timeout=300,
        )
        run([sys.executable, str(assets / "patch_xray.py"), str(source), "--assets", str(assets)])
        go_mod = (source / "go.mod").read_text(encoding="utf-8")
        match = re.search(r"(?m)^go\s+(\d+\.\d+(?:\.\d+)?)\s*$", go_mod)
        if not match:
            raise CoreManagerError("cannot determine the Go toolchain required by this Xray release")
        go_version = match.group(1)
        image = f"golang:{go_version}-bookworm"
        module_cache = CACHE_ROOT / "go-mod"
        build_cache = CACHE_ROOT / "go-build"
        module_cache.mkdir(parents=True, exist_ok=True)
        build_cache.mkdir(parents=True, exist_ok=True)
        print(f"core-build: testing patch with {image}", flush=True)
        mounts = [
            "-v", f"{source}:/src",
            "-v", f"{output}:/out",
            "-v", f"{module_cache}:/go/pkg/mod",
            "-v", f"{build_cache}:/root/.cache/go-build",
            "-w", "/src",
        ]
        run(["docker", "run", "--rm", *mounts, image, "gofmt", "-w",
             "transport/internet/splithttp/hub.go",
             "transport/internet/splithttp/upload_queue.go",
             "transport/internet/splithttp/xhttp_cleaner_reaper.go",
             "transport/internet/splithttp/xhttp_cleaner_reaper_test.go"], capture=False, timeout=300)
        run(["docker", "run", "--rm", *mounts, image, "go", "test",
             "./transport/internet/splithttp"], capture=False, timeout=900)
        run(["docker", "run", "--rm", *mounts, image, "go", "test", "-race",
             "-run", "^TestXHTTPCleaner", "./transport/internet/splithttp"],
            capture=False, timeout=900)
        marker = f"{PATCH_ID}-{info['version']}"
        build_script = (
            f"CGO_ENABLED=0 GOOS=linux GOARCH={info['goarch']} "
            "go build -trimpath -buildvcs=false "
            f"-ldflags='-s -w -X github.com/xtls/xray-core/core.build={marker}' "
            "-o /out/xray ./main"
        )
        run(["docker", "run", "--rm", *mounts, image, "bash", "-ceu", build_script],
            capture=False, timeout=1200)
        built = output / "xray"
        if not built.is_file() or built.stat().st_size < 1024 * 1024:
            raise CoreManagerError("builder did not produce a plausible Xray binary")
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = destination.with_name(f".{destination.name}.new")
        shutil.copy2(built, staging)
        os.chmod(staging, 0o755)
        os.replace(staging, destination)
        atomic_json(
            destination.parent / "build.json",
            {
                "patch_id": PATCH_ID,
                "xray_version": info["version"],
                "architecture": info["arch"],
                "sha256": sha256(destination),
                "built_at": int(time.time()),
                "source_repository": XRAY_REPOSITORY,
                "source_tag": tag,
                "go_image": image,
            },
        )


def dump_runtime_config(container: str, destination: Path) -> None:
    attempts = (
        ["docker", "exec", container, "cli", "--dump-config-raw"],
        ["docker", "exec", container, "cli", "-D"],
    )
    output = ""
    for command in attempts:
        completed = run(command, check=False)
        if completed.returncode == 0 and (completed.stdout or "").strip():
            output = (completed.stdout or "").strip()
            break
    first, last = output.find("{"), output.rfind("}")
    if first < 0 or last <= first:
        raise CoreManagerError("cannot obtain the active RemnaNode Xray configuration")
    try:
        json.loads(output[first : last + 1])
    except json.JSONDecodeError as exc:
        raise CoreManagerError("the active Xray configuration is not valid JSON") from exc
    destination.write_text(output[first : last + 1] + "\n", encoding="utf-8")
    os.chmod(destination, 0o600)


def copy_from_container(container: str, source: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(destination.parent, 0o700)
    temporary = destination.with_name(f".{destination.name}.new")
    try:
        run(["docker", "cp", f"{container}:{source}", str(temporary)])
        os.chmod(temporary, 0o700)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def wait_healthy(container: str, expected_marker: str | None, timeout: int = 90) -> None:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        running = docker(container, "inspect", "-f", "{{.State.Running}}", container, check=False)
        health = docker(container, "inspect", "-f", "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}", container, check=False)
        top = docker(container, "top", container, "-eo", "comm,args", check=False)
        process_ok = bool(re.search(r"(?m)(^|\s)(rw-core|xray)(\s|$)|/usr/local/bin/(rw-core|xray)", top))
        marker_ok = True
        if expected_marker is not None:
            try:
                marker_ok = expected_marker in version_statement(container, core_path(container))
            except CoreManagerError:
                marker_ok = False
        last = f"running={running} health={health} xray_process={process_ok} marker={marker_ok}"
        if running == "true" and process_ok and marker_ok and health in ("none", "healthy"):
            return
        if health == "unhealthy" or running == "false":
            break
        time.sleep(2)
    raise CoreManagerError(f"container did not become healthy after core restart: {last}")


def stock_core_is_running(container: str) -> bool:
    try:
        if docker(container, "inspect", "-f", "{{.State.Running}}", container, check=False) != "true":
            return False
        info = current_info(container)
        top = docker(container, "top", container, "-eo", "comm,args", check=False)
        process_ok = bool(
            re.search(r"(?m)(^|\s)(rw-core|xray)(\s|$)|/usr/local/bin/(rw-core|xray)", top)
        )
        return info["patched"] == "false" and process_ok
    except (CoreManagerError, OSError):
        return False


def validate_inside_container(container: str, artifact: Path) -> str:
    token = f"xhttp-cleaner-{os.getpid()}"
    remote_binary = f"/tmp/{token}.xray"
    remote_config = f"/tmp/{token}.json"
    with tempfile.TemporaryDirectory(prefix="xhttp-cleaner-config-") as temporary:
        config = Path(temporary) / "active.json"
        dump_runtime_config(container, config)
        try:
            run(["docker", "cp", str(artifact), f"{container}:{remote_binary}"])
            run(["docker", "cp", str(config), f"{container}:{remote_config}"])
            run(["docker", "exec", container, "chmod", "0755", remote_binary])
            statement = docker(container, "exec", container, remote_binary, "version")
            if PATCH_ID not in statement:
                raise CoreManagerError("built binary does not contain the XHTTP Cleaner build marker")
            run(["docker", "exec", container, remote_binary, "run", "-test", "-config", remote_config], timeout=120)
            return remote_binary
        except BaseException:
            run(["docker", "exec", container, "rm", "-f", remote_binary, remote_config], check=False)
            raise
        finally:
            run(["docker", "exec", container, "rm", "-f", remote_config], check=False)


def deploy(container: str, info: dict[str, str], artifact: Path) -> None:
    live = current_info(container)
    identity_fields = ("container_id", "version", "arch", "binary")
    if any(live[field] != info[field] for field in identity_fields) or live["patched"] != "false":
        raise CoreManagerError("container or Xray version changed while the fork was being built")
    directory = version_dir(info)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    backup = directory / "backups" / info["container_id"] / "stock-xray"
    inspect_file = directory / f"container-inspect-{info['container_id'][:12]}.json"
    inspect_raw = run(["docker", "inspect", container]).stdout or "[]"
    settings_before = preserved_container_settings(inspect_raw)
    if not inspect_file.exists():
        inspect_file.write_text(inspect_raw, encoding="utf-8")
        os.chmod(inspect_file, 0o600)
    # deploy() is entered only for a stock core. Refresh the per-container
    # backup on every deployment so it is byte-for-byte the current original.
    copy_from_container(container, info["binary"], backup)
    if PATCH_ID in version_statement(container, info["binary"]):
        backup.unlink(missing_ok=True)
        raise CoreManagerError("refusing to record an already patched binary as the stock rollback copy")

    remote_binary = validate_inside_container(container, artifact)
    replaced = False
    try:
        if docker(container, "inspect", "-f", "{{.Id}}", container) != info["container_id"]:
            raise CoreManagerError("container was recreated immediately before core replacement")
        run(["docker", "exec", container, "mv", remote_binary, info["binary"]])
        replaced = True
        run(["docker", "restart", container], timeout=120)
        current_id = docker(container, "inspect", "-f", "{{.Id}}", container)
        if current_id != info["container_id"]:
            raise CoreManagerError("container identity changed unexpectedly during restart")
        settings_after = preserved_container_settings(run(["docker", "inspect", container]).stdout or "[]")
        if settings_after != settings_before:
            raise CoreManagerError("Docker container settings changed unexpectedly during restart")
        wait_healthy(container, PATCH_ID)
        atomic_json(
            STATE_ROOT / "deployment.json",
            {
                "patch_id": PATCH_ID,
                "container": container,
                "container_id": current_id,
                "binary": info["binary"],
                "xray_version": info["version"],
                "architecture": info["arch"],
                "artifact": str(artifact),
                "artifact_sha256": sha256(artifact),
                "backup": str(backup),
                "backup_sha256": sha256(backup),
                "deployed_at": int(time.time()),
            },
        )
        print(f"core-deploy: Xray {info['version']} patched; the same container was restarted", flush=True)
    except BaseException as exc:
        if replaced:
            print(f"core-deploy: validation failed, rolling back: {exc}", file=sys.stderr, flush=True)
            rollback_binary(container, info["binary"], backup, info["container_id"])
        raise


def rollback_binary(container: str, binary: str, backup: Path, expected_container_id: str) -> None:
    if not backup.is_file():
        raise CoreManagerError(f"stock rollback binary is missing: {backup}")
    if docker(container, "inspect", "-f", "{{.Id}}", container) != expected_container_id:
        raise CoreManagerError("container was recreated; refusing to inject an old rollback binary")
    remote = f"/tmp/xhttp-cleaner-rollback-{os.getpid()}"
    run(["docker", "cp", str(backup), f"{container}:{remote}"])
    run(["docker", "exec", container, "chmod", "0755", remote])
    run(["docker", "exec", container, "mv", remote, binary])
    run(["docker", "restart", container], timeout=120)
    if docker(container, "inspect", "-f", "{{.Id}}", container) != expected_container_id:
        raise CoreManagerError("container identity changed during rollback")
    wait_healthy(container, None)


def ensure(container: str, *, retry_failed: bool = False) -> str:
    require_environment(container)
    info = current_info(container)
    if info["patched"] == "true":
        print(f"core-status: patched Xray {info['version']} ({PATCH_ID})")
        return "already-patched"
    artifact = artifact_path(info)
    failure = STATE_ROOT / "failures" / f"{PATCH_ID}-{info['version']}-{info['arch']}.json"
    if failure.exists() and not retry_failed:
        raise CoreManagerError(
            f"this exact Xray version previously failed the compatibility gate; "
            f"stock core was preserved ({failure})"
        )
    try:
        if not artifact.is_file():
            if shutil.which("git") is None:
                raise CoreManagerError("required executable is missing: git")
            clone_and_build(info, artifact)
        deploy(container, info, artifact)
        failure.unlink(missing_ok=True)
    except BaseException as exc:
        stock_preserved = stock_core_is_running(container)
        atomic_json(
            failure,
            {
                "patch_id": PATCH_ID,
                "xray_version": info["version"],
                "architecture": info["arch"],
                "failed_at": int(time.time()),
                "error": str(exc),
                "stock_core_preserved": stock_preserved,
            },
        )
        raise
    return "deployed"


def rollback(container: str) -> None:
    require_environment(container)
    metadata_path = STATE_ROOT / "deployment.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CoreManagerError(f"deployment metadata is unavailable: {exc}") from exc
    if metadata.get("container") != container:
        raise CoreManagerError("rollback metadata belongs to another container")
    current_id = docker(container, "inspect", "-f", "{{.Id}}", container)
    if metadata.get("container_id") != current_id:
        raise CoreManagerError("container was recreated; automatic rollback is intentionally refused")
    backup = Path(str(metadata["backup"]))
    if sha256(backup) != metadata.get("backup_sha256"):
        raise CoreManagerError("rollback binary checksum mismatch")
    rollback_binary(container, str(metadata["binary"]), backup, current_id)
    print("core-rollback: restored the original Xray binary and restarted the same container")


def restore_if_patched(container: str) -> None:
    require_environment(container)
    info = current_info(container)
    if info["patched"] != "true":
        print("core-restore: current Xray is already the original stock build")
        return
    rollback(container)


def print_status(container: str) -> None:
    require_environment(container)
    info = current_info(container)
    artifact = artifact_path(info)
    print(f"core_container={container}")
    print(f"core_container_id={info['container_id'][:12]}")
    print(f"core_binary={info['binary']}")
    print(f"core_version={info['version']}")
    print(f"core_arch={info['arch']}")
    print(f"core_patch_id={PATCH_ID}")
    print(f"core_patched={info['patched']}")
    print(f"core_artifact_cached={str(artifact.is_file()).lower()}")


def main() -> int:
    parser = argparse.ArgumentParser(description="XHTTP Cleaner Xray core manager")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    ensure_parser = subparsers.add_parser("ensure")
    ensure_parser.add_argument("--retry-failed", action="store_true")
    ensure_parser.add_argument("--nonfatal", action="store_true")
    subparsers.add_parser("rollback")
    subparsers.add_parser("restore-if-patched")
    args = parser.parse_args()
    try:
        container = load_container(args.config)
        if args.command == "status":
            print_status(container)
        elif args.command == "ensure":
            with operation_lock():
                try:
                    ensure(container, retry_failed=args.retry_failed)
                except CoreManagerError as exc:
                    if args.nonfatal and stock_core_is_running(container):
                        print(f"core-ensure: {exc}; current stock core was left untouched", file=sys.stderr)
                        return 0
                    raise
        elif args.command == "rollback":
            with operation_lock():
                rollback(container)
        elif args.command == "restore-if-patched":
            with operation_lock():
                restore_if_patched(container)
        return 0
    except (CoreManagerError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"xray-core-manager: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
