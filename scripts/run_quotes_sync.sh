#!/bin/sh

set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR="/Users/zhangyun/Documents/code/planet-stock-analyzer"
if [ -f "$SCRIPT_DIR/sync_quotes.py" ]; then
  SYNC_SCRIPT="$SCRIPT_DIR/sync_quotes.py"
else
  SYNC_SCRIPT="$PROJECT_DIR/scripts/sync_quotes.py"
fi

LOG_DIR="${QUOTE_LOG_DIR:-$HOME/Library/Logs/planet-stock-analyzer}"
LOG_FILE="${QUOTE_LOG_FILE:-$LOG_DIR/quotes-sync.log}"
STATE_DIR="${QUOTE_STATE_DIR:-$HOME/Library/Application Support/planet-stock-analyzer-quotes/state}"
LOCK_DIR="${QUOTE_LOCK_DIR:-$LOG_DIR/.quotes-sync.lock}"
APP_URL="${PLANET_APP_URL:-http://127.0.0.1:8000}"
BATCH_SIZE="${QUOTE_BATCH_SIZE:-10}"
BATCHES="${QUOTE_BATCHES:-600}"

umask 077
if ! mkdir -p "$LOG_DIR" "$STATE_DIR"; then
  exit 1
fi

# The calendar job is weekdays only; keep the guard for manual runs too.
case "$(date '+%u')" in
  1|2|3|4|5) ;;
  *)
    printf '[%s] skipped: weekend\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" >> "$LOG_FILE"
    exit 0
    ;;
esac

# ponytail: one global quote lock; use per-market locks only if multiple workers are added.
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  printf '[%s] skipped: another quote sync is running\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" >> "$LOG_FILE"
  exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT INT TERM

SYNC_DATE=$(date '+%Y-%m-%d')
STATE_FILE="$STATE_DIR/quote-sync-all-$SYNC_DATE.json"
printf '[%s] start date=%s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$SYNC_DATE" >> "$LOG_FILE"
/usr/bin/python3 "$SYNC_SCRIPT" \
  --app-url "$APP_URL" \
  --all-stocks \
  --start-date "$SYNC_DATE" \
  --end-date "$SYNC_DATE" \
  --batch-size "$BATCH_SIZE" \
  --batches "$BATCHES" \
  --state-file "$STATE_FILE" >> "$LOG_FILE" 2>&1
status=$?
printf '[%s] exit=%s date=%s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$status" "$SYNC_DATE" >> "$LOG_FILE"
exit "$status"
