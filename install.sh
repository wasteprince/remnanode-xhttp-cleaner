#!/usr/bin/env bash

set -Eeuo pipefail

readonly PROGRAM_NAME="remnanode-xhttp-clean-installer"
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly CLEANER_SOURCE="$SCRIPT_DIR/remnanode-xhttp-clean.py"
readonly MENU_SOURCE="$SCRIPT_DIR/xhttp-cleaner-menu.sh"
readonly CORE_MANAGER_SOURCE="$SCRIPT_DIR/xray-core-manager.py"
readonly XRAY_PATCH_SOURCE="$SCRIPT_DIR/xray_patch"
readonly CLEANER_INSTALLED="/usr/local/sbin/remnanode-xhttp-clean"
readonly MENU_INSTALLED="/usr/local/bin/xhttp-cleaner"
readonly CORE_MANAGER_INSTALLED="/usr/local/lib/remnanode-xhttp-clean/xray-core-manager"
readonly XRAY_PATCH_INSTALLED="/usr/local/lib/remnanode-xhttp-clean/xray_patch"
readonly CONFIG_FILE="/etc/remnanode-xhttp-clean.json"
readonly SERVICE_NAME="remnanode-xhttp-clean.service"
readonly TIMER_NAME="remnanode-xhttp-clean.timer"

CONTAINER_NAME="${REMNANODE_CONTAINER:-remnanode}"

log() {
    printf '%s [%s] %s\n' "$(date --iso-8601=seconds)" "$1" "$2"
}

die() {
    log ERROR "$1" >&2
    exit 1
}

on_error() {
    local exit_code=$?
    log ERROR "Установка прервана на строке ${BASH_LINENO[0]} (код $exit_code)." >&2
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --no-pager --full status "$SERVICE_NAME" 2>/dev/null || true
    fi
    exit "$exit_code"
}
trap on_error ERR

require_root() {
    [[ ${EUID:-$(id -u)} -eq 0 ]] || die "Запустите установщик через sudo или от root."
}

check_ubuntu() {
    [[ -r /etc/os-release ]] || die "Не удалось определить операционную систему."
    # shellcheck source=/dev/null
    source /etc/os-release
    [[ "${ID:-}" == "ubuntu" ]] || die "Этот установщик поддерживает Ubuntu; обнаружено: ${ID:-unknown}."
}

install_dependencies() {
    log INFO "Обновление индекса APT..."
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    log INFO "Установка Python 3, Git, util-linux и CA-сертификатов..."
    apt-get install -y --no-install-recommends python3 git util-linux ca-certificates

    command -v python3 >/dev/null 2>&1 || die "python3 не найден после установки."
    command -v git >/dev/null 2>&1 || die "git не найден после установки."
    command -v nsenter >/dev/null 2>&1 || die "nsenter не найден после установки util-linux."
    command -v systemctl >/dev/null 2>&1 || die "systemd не найден."
}

check_cleaner_source() {
    [[ -r "$CLEANER_SOURCE" ]] || die "Рядом с install.sh отсутствует remnanode-xhttp-clean.py."
    [[ -r "$MENU_SOURCE" ]] || die "Рядом с install.sh отсутствует xhttp-cleaner-menu.sh."
    [[ -r "$CORE_MANAGER_SOURCE" ]] || die "Рядом с install.sh отсутствует xray-core-manager.py."
    [[ -r "$XRAY_PATCH_SOURCE/patch_xray.py" ]] || die "Отсутствуют файлы патча Xray."
    bash -n "$MENU_SOURCE"
    python3 - "$CLEANER_SOURCE" "$CORE_MANAGER_SOURCE" "$XRAY_PATCH_SOURCE/patch_xray.py" <<'PY'
from pathlib import Path
import sys

for filename in sys.argv[1:]:
    source = Path(filename).read_text(encoding="utf-8")
    compile(source, filename, "exec")
PY
}

detect_container() {
    command -v docker >/dev/null 2>&1 || die \
        "Docker не найден. Сначала установите и запустите RemnaNode, затем повторите установку."
    docker info >/dev/null 2>&1 || die "Docker daemon недоступен."

    if [[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null || true)" == "true" ]]; then
        return 0
    fi

    local detected="" count=0 name image
    while IFS=$'\t' read -r name image; do
        [[ "$image" == remnawave/node:* || "$image" == remnawave/node@* || "$image" == "remnawave/node" ]] || continue
        detected="$name"
        count=$((count + 1))
    done < <(docker ps --format '{{.Names}}\t{{.Image}}')

    if (( count == 1 )); then
        CONTAINER_NAME="$detected"
        log INFO "Автоматически найден контейнер RemnaNode: $CONTAINER_NAME"
        return 0
    fi
    if (( count > 1 )); then
        die "Найдено несколько контейнеров remnawave/node. Укажите нужный: REMNANODE_CONTAINER=имя sudo -E ./install.sh"
    fi
    die "Запущенный контейнер RemnaNode не найден. Проверено имя: $CONTAINER_NAME"
}

write_initial_config() {
    if [[ -e "$CONFIG_FILE" ]]; then
        python3 - "$CONFIG_FILE" <<'PY'
import json
import os
import sys
import tempfile

path = sys.argv[1]
with open(path, encoding="utf-8") as handle:
    config = json.load(handle)

# v3 scanned every ESTABLISHED socket and could terminate a valid idle TCP
# bridge. Migrate only old configs which do not yet contain the v4 switch;
# explicit v4 operator choices are preserved on reinstall.
if "clean_established_outbound" not in config:
    config["clean_established_outbound"] = False
    config["clean_xhttp_buffers"] = False
config.setdefault("clean_close_wait", True)

directory = os.path.dirname(path)
fd, temporary = tempfile.mkstemp(prefix=".remnanode-xhttp-clean.", dir=directory)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o640)
    os.replace(temporary, path)
except BaseException:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise
PY
        log INFO "Существующая конфигурация мигрирована/сохранена: $CONFIG_FILE"
        return 0
    fi

    local temporary
    temporary="$(mktemp /etc/.remnanode-xhttp-clean.json.XXXXXX)"
    chmod 0640 "$temporary"
    python3 - "$temporary" "$CONTAINER_NAME" <<'PY'
import json
import sys

path, container = sys.argv[1:]
config = {
    "container": container,
    "idle_seconds": 300,
    "include_inbound": False,
    "exclude_loopback": True,
    "clean_xhttp_buffers": False,
    "clean_close_wait": True,
    "clean_established_outbound": False,
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(config, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY
    mv -f "$temporary" "$CONFIG_FILE"
    log INFO "Создана конфигурация с минимальным простоем 300 секунд: $CONFIG_FILE"
}

install_cleaner() {
    write_initial_config
    log INFO "Установка очистителя, core manager и systemd units..."
    install -d -m 0755 "$(dirname -- "$CORE_MANAGER_INSTALLED")"
    install -m 0755 "$CORE_MANAGER_SOURCE" "$CORE_MANAGER_INSTALLED"
    install -d -m 0755 "$XRAY_PATCH_INSTALLED"
    install -m 0755 "$XRAY_PATCH_SOURCE/patch_xray.py" "$XRAY_PATCH_INSTALLED/patch_xray.py"
    install -m 0644 "$XRAY_PATCH_SOURCE/xhttp_cleaner_reaper.go" "$XRAY_PATCH_INSTALLED/xhttp_cleaner_reaper.go"
    install -m 0644 "$XRAY_PATCH_SOURCE/xhttp_cleaner_reaper_test.go" "$XRAY_PATCH_INSTALLED/xhttp_cleaner_reaper_test.go"
    install -m 0644 "$XRAY_PATCH_SOURCE/core_memory_optimizer.go" "$XRAY_PATCH_INSTALLED/core_memory_optimizer.go"
    install -m 0644 "$XRAY_PATCH_SOURCE/core_memory_optimizer_test.go" "$XRAY_PATCH_INSTALLED/core_memory_optimizer_test.go"
    python3 "$CLEANER_SOURCE" install
    install -m 0755 "$MENU_SOURCE" "$MENU_INSTALLED"

    [[ -x "$CLEANER_INSTALLED" ]] || die "Установленный очиститель не найден: $CLEANER_INSTALLED"
    [[ -x "$MENU_INSTALLED" ]] || die "Команда управления не установлена: $MENU_INSTALLED"
    systemctl is-enabled --quiet "$TIMER_NAME" || die "Timer не включён."
    systemctl is-active --quiet "$TIMER_NAME" || die "Timer не запущен."
}

install_patched_core() {
    log INFO "Сборка форка точно для версии Xray из контейнера $CONTAINER_NAME..."
    "$CORE_MANAGER_INSTALLED" ensure --retry-failed
}

run_initial_cleanup() {
    log INFO "Первый запуск очистки..."
    systemctl start "$SERVICE_NAME"
    if systemctl is-failed --quiet "$SERVICE_NAME"; then
        die "Первый запуск службы завершился ошибкой."
    fi
    return 0
}

show_result() {
    log INFO "Установка завершена. XHTTP Cleaner by Bankaev и внутренний reaper Xray активны."
    printf '\n'
    systemctl --no-pager --full status "$TIMER_NAME" | sed -n '1,14p'
    printf '\nПоследний запуск:\n'
    journalctl -u "$SERVICE_NAME" -n 20 --no-pager || true
    printf '\nПолезные команды:\n'
    printf '  sudo %s status\n' "$CLEANER_INSTALLED"
    printf '  sudo %s scan\n' "$CLEANER_INSTALLED"
    printf '  sudo systemctl status %s\n' "$TIMER_NAME"
    printf '  sudo journalctl -fu %s\n' "$SERVICE_NAME"
    printf '\nПанель управления:\n'
    printf '  xhttp-cleaner\n'
}

main() {
    require_root
    check_ubuntu
    check_cleaner_source
    install_dependencies
    detect_container
    install_cleaner
    install_patched_core
    run_initial_cleanup
    show_result
}

main "$@"
