#!/bin/sh
# make export — everything needed to analyse the station off the Pi, in one
# archive in the home directory: ~/airstation-<YYYYMMDD-HHMM>.tar.gz
#
#   db/airstation.db            consistent copy (SQLite online backup)
#   logs/                       every app log file (daily, 45 days)
#   journal/units.txt           journalctl of the three units, last 30 days
#   journal/kernel-current.txt  dmesg of this boot (I2C, under-voltage, Wi-Fi)
#   journal/kernel-previous.txt dmesg of the previous boot (why did it reboot?)
#   system/                     throttled, temp, df, free, uname
#   config.toml, commit.txt
#
# Usage: tools/export.sh [--config path] [--out dir]   (both default: repo / $HOME)
set -eu

REPO="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="$REPO/config.toml"
OUT="$HOME"
while [ $# -gt 0 ]; do
    case "$1" in
        --config) CONFIG="$2"; shift 2 ;;
        --out) OUT="$2"; shift 2 ;;
        *) echo "usage: tools/export.sh [--config path] [--out dir]" >&2; exit 2 ;;
    esac
done

PY="$REPO/.venv/bin/python"
[ -x "$PY" ] || PY=python3
NAME="airstation-$(date +%Y%m%d-%H%M)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
TREE="$WORK/$NAME"
mkdir -p "$TREE/db" "$TREE/logs" "$TREE/journal" "$TREE/system"

cd "$REPO"
echo "- database"
"$PY" -m tools.backup --config "$CONFIG" "$TREE/db/airstation.db"

echo "- logs"
LOGS="$("$PY" -c "import sys; from shared.config import Config; print(Config.load(sys.argv[1]).paths.logs)" "$CONFIG")"
if [ -d "$LOGS" ]; then cp "$LOGS"/* "$TREE/logs/" 2>/dev/null || echo "  (no log files yet)"; else echo "  (no log directory)"; fi

echo "- journal"
journalctl -u airstation-collector -u airstation-manager -u airstation-dashboard --since -30d --no-pager \
    > "$TREE/journal/units.txt" 2>&1 || echo "  (journalctl units: unavailable)"
journalctl -k -b 0 --no-pager > "$TREE/journal/kernel-current.txt" 2>&1 || echo "  (journalctl -k -b 0: unavailable)"
journalctl -k -b -1 --no-pager > "$TREE/journal/kernel-previous.txt" 2>&1 || echo "  (journalctl -k -b -1: unavailable)"

echo "- system"
{ vcgencmd get_throttled 2>&1 || echo unavailable; } > "$TREE/system/throttled.txt"
{ vcgencmd measure_temp 2>&1 || echo unavailable; } > "$TREE/system/temp.txt"
{ df -h 2>&1 || echo unavailable; } > "$TREE/system/df.txt"
{ free -m 2>&1 || echo unavailable; } > "$TREE/system/free.txt"
{ uname -a 2>&1 || echo unavailable; } > "$TREE/system/uname.txt"
cp "$CONFIG" "$TREE/config.toml"
{ git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown; } > "$TREE/commit.txt"

mkdir -p "$OUT"
tar -czf "$OUT/$NAME.tar.gz" -C "$WORK" "$NAME"
echo "archive: $OUT/$NAME.tar.gz ($(du -h "$OUT/$NAME.tar.gz" | cut -f1))"
echo "move it to the laptop, then to the server: scp $OUT/$NAME.tar.gz <laptop>: ; make agent-import FILE=$NAME.tar.gz"
