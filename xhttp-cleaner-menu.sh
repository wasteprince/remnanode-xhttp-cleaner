#!/usr/bin/env bash

set -Eeuo pipefail

readonly APP_NAME="XHTTP Cleaner"
readonly AUTHOR="Bankaev"
readonly VERSION="3.0.1"
readonly CLEANER="/usr/local/sbin/remnanode-xhttp-clean"
readonly CORE_MANAGER="/usr/local/lib/remnanode-xhttp-clean/xray-core-manager"
readonly PROJECT_DIR="/opt/node-xhttp"
readonly INSTALLER="$PROJECT_DIR/install.sh"
readonly SERVICE="remnanode-xhttp-clean.service"
readonly TIMER="remnanode-xhttp-clean.timer"
readonly CONFIG="/etc/remnanode-xhttp-clean.json"

if [[ -t 1 ]]; then
    readonly RESET=$'\033[0m'
    readonly GOLD=$'\033[38;5;220m'
    readonly ORANGE=$'\033[38;5;214m'
    readonly GREEN=$'\033[38;5;82m'
    readonly RED=$'\033[38;5;196m'
    readonly GRAY=$'\033[38;5;245m'
    readonly WHITE=$'\033[38;5;255m'
else
    readonly RESET="" GOLD="" ORANGE="" GREEN="" RED="" GRAY="" WHITE=""
fi

ensure_root() {
    if (( EUID != 0 )); then
        command -v sudo >/dev/null 2>&1 || {
            printf 'Нужны права root.\n' >&2
            exit 1
        }
        exec sudo -- "$0" "$@"
    fi
}

clear_screen() {
    [[ -t 1 && -n "${TERM:-}" ]] && clear || true
}

cpu_snapshot() {
    awk '/^cpu / { idle=$5+$6; total=0; for (i=2;i<=NF;i++) total+=$i; print total, idle; exit }' /proc/stat
}

cpu_percent() {
    local total1 idle1 total2 idle2 delta_total delta_idle
    read -r total1 idle1 < <(cpu_snapshot)
    sleep 0.15
    read -r total2 idle2 < <(cpu_snapshot)
    delta_total=$((total2 - total1))
    delta_idle=$((idle2 - idle1))
    if (( delta_total <= 0 )); then
        printf '0'
    else
        printf '%s' "$((100 * (delta_total - delta_idle) / delta_total))"
    fi
}

ram_stats() {
    awk '
        /^MemTotal:/ { total=$2 }
        /^MemAvailable:/ { available=$2 }
        END {
            used=total-available
            percent=(total > 0 ? used*100/total : 0)
            printf "%d %.1f %.1f\n", percent, used/1048576, total/1048576
        }
    ' /proc/meminfo
}

disk_stats() {
    df -Pk / | awk 'NR==2 { gsub(/%/, "", $5); printf "%d %.1f %.1f\n", $5, $3/1048576, $2/1048576 }'
}

progress_bar() {
    local value="$1" width=10 filled empty result="" i color="$GREEN"
    (( value >= 70 )) && color="$RED"
    (( value >= 45 && value < 70 )) && color="$GOLD"
    filled=$((value * width / 100))
    (( filled > width )) && filled=$width
    empty=$((width - filled))
    for ((i=0; i<filled; i++)); do result+="■"; done
    for ((i=0; i<empty; i++)); do result+="□"; done
    printf '[%s%s%s]' "$color" "$result" "$RESET"
}

status_value() {
    local key="$1" content="$2"
    awk -F= -v key="$key" '$1 == key { sub(/^[^=]*=/, ""); print; exit }' <<<"$content"
}

draw_dashboard() {
    local cpu ram disk ram_pct ram_used ram_total disk_pct disk_used disk_total cores
    local timer_active timer_enabled cleaner_status container image rss sockets stale xhttp_stale idle ports
    local xhttp_ports xhttp_discovery core_status core_version core_patched
    local last_result last_closed next_run cleaner_state cleaner_color

    cpu="$(cpu_percent)"
    read -r ram_pct ram_used ram_total < <(ram_stats)
    read -r disk_pct disk_used disk_total < <(disk_stats)
    cores="$(nproc)"
    timer_active="$(systemctl is-active "$TIMER" 2>/dev/null || true)"
    timer_enabled="$(systemctl is-enabled "$TIMER" 2>/dev/null || true)"
    if [[ "$timer_active" == "active" ]]; then
        cleaner_state="РАБОТАЕТ ($timer_enabled)"
        cleaner_color="$GREEN"
    else
        cleaner_state="ВЫКЛЮЧЕН ($timer_enabled)"
        cleaner_color="$RED"
    fi
    cleaner_status="$($CLEANER status 2>&1 || true)"
    core_status="$($CORE_MANAGER status 2>&1 || true)"
    container="$(status_value container "$cleaner_status")"
    image="$(status_value image "$cleaner_status")"
    rss="$(status_value xray_rss_mb "$cleaner_status")"
    sockets="$(status_value owned_tcp_sockets "$cleaner_status")"
    stale="$(status_value stale_outbound_sockets "$cleaner_status")"
    xhttp_stale="$(status_value stale_xhttp_sockets "$cleaner_status")"
    xhttp_ports="$(status_value xhttp_listeners "$cleaner_status")"
    xhttp_discovery="$(status_value xhttp_discovery "$cleaner_status")"
    idle="$(status_value idle_seconds "$cleaner_status")"
    ports="$(status_value listening_ports "$cleaner_status")"
    core_version="$(status_value core_version "$core_status")"
    core_patched="$(status_value core_patched "$core_status")"
    last_result="$(systemctl show "$SERVICE" -p Result --value 2>/dev/null || true)"
    last_closed="$(journalctl -u "$SERVICE" -n 300 --no-pager 2>/dev/null | sed -n 's/.*Закрыто по inode + kernel cookie: //p' | tail -n 1)"
    next_run="$(systemctl list-timers "$TIMER" --no-legend --no-pager 2>/dev/null | awk 'NF { print $1, $2, $3, $4; exit }')"

    printf '%s╔══════════════════════════════════════════════════════════════╗%s\n' "$GOLD" "$RESET"
    printf '%s║%s  %s%s v%s%s — безопасная очистка сокетов             %s║%s\n' "$GOLD" "$RESET" "$WHITE" "$APP_NAME" "$VERSION" "$RESET" "$GOLD" "$RESET"
    printf '%s║%s  by %s%s%s                                                   %s║%s\n' "$GOLD" "$RESET" "$ORANGE" "$AUTHOR" "$RESET" "$GOLD" "$RESET"
    printf '%s╠══════════════════════════════════════════════════════════════╣%s\n' "$GOLD" "$RESET"
    printf '%s║%s Загрузка CPU       : %-24s %3s%%  (%s vCore) %s║%s\n' "$GOLD" "$RESET" "$(progress_bar "$cpu")" "$cpu" "$cores" "$GOLD" "$RESET"
    printf '%s║%s Память (RAM)       : %-24s %3s%%  (%s / %sG) %s║%s\n' "$GOLD" "$RESET" "$(progress_bar "$ram_pct")" "$ram_pct" "$ram_used" "$ram_total" "$GOLD" "$RESET"
    printf '%s║%s Диск (/)           : %-24s %3s%%  (%s / %sG) %s║%s\n' "$GOLD" "$RESET" "$(progress_bar "$disk_pct")" "$disk_pct" "$disk_used" "$disk_total" "$GOLD" "$RESET"
    printf '%s╠─[ STATUS ]───────────────────────────────────────────────────╣%s\n' "$GOLD" "$RESET"
    printf '%s║%s Очиститель         : %s%-39s%s%s║%s\n' "$GOLD" "$RESET" "$cleaner_color" "$cleaner_state" "$RESET" "$GOLD" "$RESET"
    printf '%s║%s RemnaNode          : %-39s%s║%s\n' "$GOLD" "$RESET" "${container:-недоступен} ${image:-}" "$GOLD" "$RESET"
    printf '%s║%s Xray Core Fork     : %-39s%s║%s\n' "$GOLD" "$RESET" "v${core_version:-?}; patched=${core_patched:-?}" "$GOLD" "$RESET"
    printf '%s║%s Xray RSS           : %-39s%s║%s\n' "$GOLD" "$RESET" "${rss:-?} MiB" "$GOLD" "$RESET"
    printf '%s║%s TCP-сокеты Xray    : %-39s%s║%s\n' "$GOLD" "$RESET" "${sockets:-?}" "$GOLD" "$RESET"
    printf '%s║%s Старые outbound    : %-39s%s║%s\n' "$GOLD" "$RESET" "${stale:-?}" "$GOLD" "$RESET"
    printf '%s║%s Старые XHTTP TCP   : %-39s%s║%s\n' "$GOLD" "$RESET" "${xhttp_stale:-?}" "$GOLD" "$RESET"
    printf '%s║%s XHTTP listeners    : %-39s%s║%s\n' "$GOLD" "$RESET" "${xhttp_ports:-нет} (${xhttp_discovery:-?})" "$GOLD" "$RESET"
    printf '%s║%s Listening-порты    : %-39s%s║%s\n' "$GOLD" "$RESET" "${ports:-?}" "$GOLD" "$RESET"
    printf '%s║%s Последняя очистка  : %-39s%s║%s\n' "$GOLD" "$RESET" "${last_result:-нет}; закрыто ${last_closed:-0}" "$GOLD" "$RESET"
    printf '%s║%s Следующий запуск   : %-39s%s║%s\n' "$GOLD" "$RESET" "${next_run:-не назначен}" "$GOLD" "$RESET"
    printf '%s╚══════════════════════════════════════════════════════════════╝%s\n' "$GOLD" "$RESET"
}

pause_menu() {
    [[ -t 0 ]] || return 0
    printf '\n%sНажмите Enter, чтобы вернуться в меню...%s' "$GRAY" "$RESET"
    read -r _
}

show_logs() {
    journalctl -u "$SERVICE" -n 100 --no-pager
}

enable_cleaner() {
    systemctl daemon-reload
    systemctl enable --now "$TIMER"
    systemctl start "$SERVICE"
    printf '%sОчиститель включён, первая проверка выполнена.%s\n' "$GREEN" "$RESET"
}

disable_cleaner() {
    systemctl disable --now "$TIMER"
    systemctl stop "$SERVICE" 2>/dev/null || true
    printf '%sОчиститель выключен. RemnaNode не остановлен.%s\n' "$ORANGE" "$RESET"
}

run_tests() {
    [[ -d "$PROJECT_DIR" ]] || {
        printf '%sКаталог проекта %s отсутствует.%s\n' "$RED" "$PROJECT_DIR" "$RESET"
        return 1
    }
    cd "$PROJECT_DIR"
    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v tests/test_cleaner.py
    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v tests/test_core_manager.py tests/test_xray_patcher.py
    bash tests/test_install.sh
}

update_core() {
    "$CORE_MANAGER" ensure --retry-failed
}

rollback_core() {
    "$CORE_MANAGER" rollback
}

reinstall_cleaner() {
    [[ -x "$INSTALLER" ]] || {
        printf '%sУстановщик не найден: %s%s\n' "$RED" "$INSTALLER" "$RESET"
        return 1
    }
    "$INSTALLER"
}

uninstall_cleaner() {
    local confirmation
    printf '%sБудут удалены только служба XHTTP Cleaner, timer, конфигурация и команды.%s\n' "$RED" "$RESET"
    printf 'Для подтверждения введите УДАЛИТЬ: '
    read -r confirmation
    [[ "$confirmation" == "УДАЛИТЬ" ]] || {
        printf 'Отменено.\n'
        return 0
    }
    "$CLEANER" uninstall
    printf 'XHTTP Cleaner удалён. Исходники в %s сохранены для переустановки.\n' "$PROJECT_DIR"
}

show_menu() {
    printf '\n%sЧто делаем?%s\n\n' "$WHITE" "$RESET"
    printf '  %s[1]%s 📊 Обновить статус\n' "$GOLD" "$RESET"
    printf '  %s[2]%s 🔎 Показать неактивные сокеты (без изменений)\n' "$GOLD" "$RESET"
    printf '  %s[3]%s 🧹 Выполнить очистку сейчас\n' "$GOLD" "$RESET"
    printf '  %s[4]%s 📜 Последние логи\n' "$GOLD" "$RESET"
    printf '  %s[5]%s ▶  Включить программу и timer\n' "$GREEN" "$RESET"
    printf '  %s[6]%s ⏸  Выключить программу и timer\n' "$ORANGE" "$RESET"
    printf '  %s[7]%s 🧪 Запустить тесты\n' "$GOLD" "$RESET"
    printf '  %s[8]%s 🧬 Пересобрать/обновить форк Xray\n' "$GOLD" "$RESET"
    printf '  %s[9]%s ↩  Откатить оригинальное ядро Xray\n' "$ORANGE" "$RESET"
    printf '  %s[r]%s 🔄 Переустановить программу\n' "$GOLD" "$RESET"
    printf '  %s[d]%s 🗑  Удалить установленную программу\n' "$RED" "$RESET"
    printf '  %s[q]%s Выйти\n\n' "$GRAY" "$RESET"
}

interactive_menu() {
    local choice
    while true; do
        clear_screen
        draw_dashboard
        show_menu
        printf '%sВаш выбор:%s ' "$WHITE" "$RESET"
        read -r choice || return 0
        case "$choice" in
            1) continue ;;
            2) "$CLEANER" scan; pause_menu ;;
            3) "$CLEANER" clean; pause_menu ;;
            4) show_logs; pause_menu ;;
            5) enable_cleaner; pause_menu ;;
            6) disable_cleaner; pause_menu ;;
            7) run_tests; pause_menu ;;
            8) update_core; pause_menu ;;
            9) rollback_core; pause_menu ;;
            r|R|к|К) reinstall_cleaner; pause_menu ;;
            d|D|в|В) uninstall_cleaner; return 0 ;;
            q|Q|й|Й) return 0 ;;
            *) printf '%sНеизвестный пункт.%s\n' "$RED" "$RESET"; sleep 1 ;;
        esac
    done
}

usage() {
    cat <<EOF
$APP_NAME v$VERSION by $AUTHOR

Использование: xhttp-cleaner [команда]

Без команды       интерактивная панель управления
status            показать панель состояния
scan              показать старые сокеты без закрытия
clean             выполнить очистку сейчас
logs [--follow]   показать журнал службы
enable            включить timer и сразу выполнить очистку
disable           выключить timer
test              запустить тесты
core-update       проверить совместимость, собрать и установить форк Xray
core-rollback     восстановить сохранённый оригинальный Xray
reinstall         переустановить из $PROJECT_DIR
uninstall         восстановить stock Xray и удалить программу
EOF
}

main() {
    local command="${1:-menu}"
    if [[ "$command" == "help" || "$command" == "-h" || "$command" == "--help" ]]; then
        usage
        return 0
    fi
    ensure_root "$@"
    case "$command" in
        menu) interactive_menu ;;
        status) draw_dashboard ;;
        scan) "$CLEANER" scan ;;
        clean) "$CLEANER" clean ;;
        logs)
            if [[ "${2:-}" == "--follow" ]]; then
                exec journalctl -fu "$SERVICE"
            else
                show_logs
            fi
            ;;
        enable|start) enable_cleaner ;;
        disable|stop) disable_cleaner ;;
        test|tests) run_tests ;;
        core-update|core-ensure) update_core ;;
        core-rollback) rollback_core ;;
        reinstall) reinstall_cleaner ;;
        uninstall|remove) uninstall_cleaner ;;
        *) usage >&2; return 2 ;;
    esac
}

if [[ "${XHTTP_CLEANER_SOURCE_ONLY:-0}" != "1" ]]; then
    main "$@"
fi
