#!/bin/sh

set -eu

PROJECT_DIR="/Users/zhangyun/Documents/code/planet-stock-analyzer"
LABEL="com.zhangyun.planet-stock-analyzer.quotes-sync"
RUNTIME_DIR="/Users/zhangyun/Library/Application Support/planet-stock-analyzer-quotes"
LOG_DIR="/Users/zhangyun/Library/Logs/planet-stock-analyzer"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/$LABEL.plist"

mkdir -p "$PLIST_DIR" "$RUNTIME_DIR" "$RUNTIME_DIR/state" "$LOG_DIR"
cp "$PROJECT_DIR/scripts/sync_quotes.py" "$RUNTIME_DIR/sync_quotes.py"
cp "$PROJECT_DIR/scripts/run_quotes_sync.sh" "$RUNTIME_DIR/run_quotes_sync.sh"
chmod 700 "$RUNTIME_DIR/run_quotes_sync.sh"
cp "$PROJECT_DIR/launchd/$LABEL.plist" "$PLIST_PATH"

cd /
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"

printf '已安装行情同步任务 %s\n运行副本：%s\n日志：%s/quotes-sync.log\n计划：工作日 16:00（收盘后）\n' "$LABEL" "$RUNTIME_DIR" "$LOG_DIR"
