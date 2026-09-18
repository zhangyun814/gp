#!/bin/sh

set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR="/Users/zhangyun/Documents/code/planet-stock-analyzer"
if [ -f "$SCRIPT_DIR/sync_zsxq.py" ]; then
  SYNC_SCRIPT="$SCRIPT_DIR/sync_zsxq.py"
  DEFAULT_LOG_DIR="/Users/zhangyun/Library/Logs/planet-stock-analyzer"
else
  SYNC_SCRIPT="$PROJECT_DIR/scripts/sync_zsxq.py"
  DEFAULT_LOG_DIR="$PROJECT_DIR/data/logs"
fi
LOG_DIR="${ZSXQ_LOG_DIR:-$DEFAULT_LOG_DIR}"
LOG_FILE="${ZSXQ_LOG_FILE:-$LOG_DIR/zsxq-sync.log}"
LOCK_DIR="${ZSXQ_LOCK_DIR:-$LOG_DIR/.zsxq-sync.lock}"
CLI_PATH="${ZSXQ_CLI:-/usr/local/bin/zsxq-cli}"

umask 077
if ! mkdir -p "$LOG_DIR"; then
  exit 1
fi

case "$(date '+%H')" in
  06|07|08|09|10|11) ;;
  *)
    printf '[%s] skipped: outside 06:00-12:00 window\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" >> "$LOG_FILE"
    exit 0
    ;;
esac

# ponytail: one global lock; use per-account locks only if multiple sync workers are added.
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  printf '[%s] skipped: another sync is running\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" >> "$LOG_FILE"
  exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT INT TERM

printf '[%s] start\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" >> "$LOG_FILE"
/usr/bin/python3 "$SYNC_SCRIPT" \
  --cli "$CLI_PATH" \
  --pages 10 \
  --count 30 >> "$LOG_FILE" 2>&1
status=$?
printf '[%s] exit=%s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$status" >> "$LOG_FILE"
exit "$status"
