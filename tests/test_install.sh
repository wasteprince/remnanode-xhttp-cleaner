#!/usr/bin/env bash

set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

bash -n "$ROOT_DIR/install.sh"
bash -n "$ROOT_DIR/xhttp-cleaner-menu.sh"
grep -q 'idle_seconds.*300' "$ROOT_DIR/install.sh"
grep -q 'systemctl start.*SERVICE_NAME' "$ROOT_DIR/install.sh"
grep -q 'enable.*--now.*remnanode-xhttp-clean.timer' "$ROOT_DIR/remnanode-xhttp-clean.py"
grep -q 'MENU_INSTALLED="/usr/local/bin/xhttp-cleaner"' "$ROOT_DIR/install.sh"
grep -q 'AUTHOR="Bankaev"' "$ROOT_DIR/xhttp-cleaner-menu.sh"
grep -q 'CORE_MANAGER_INSTALLED.*ensure --retry-failed' "$ROOT_DIR/install.sh"
grep -q 'ExecStartPre=.*xray-core-manager ensure --nonfatal' "$ROOT_DIR/remnanode-xhttp-clean.py"
grep -q 'restore-if-patched' "$ROOT_DIR/remnanode-xhttp-clean.py"

menu_help="$($ROOT_DIR/xhttp-cleaner-menu.sh help)"
grep -q 'XHTTP Cleaner' <<<"$menu_help"
grep -q 'by Bankaev' <<<"$menu_help"

XHTTP_CLEANER_SOURCE_ONLY=1 source "$ROOT_DIR/xhttp-cleaner-menu.sh"
read -r ram_pct ram_used ram_total < <(ram_stats)
read -r disk_pct disk_used disk_total < <(disk_stats)
[[ "$ram_pct" =~ ^[0-9]+$ && "$ram_used" =~ ^[0-9]+\.[0-9]+$ && "$ram_total" =~ ^[0-9]+\.[0-9]+$ ]]
[[ "$disk_pct" =~ ^[0-9]+$ && "$disk_used" =~ ^[0-9]+\.[0-9]+$ && "$disk_total" =~ ^[0-9]+\.[0-9]+$ ]]

printf 'install.sh checks passed.\n'
