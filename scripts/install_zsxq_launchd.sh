#!/bin/sh

set -eu

PROJECT_DIR="/Users/zhangyun/Documents/code/planet-stock-analyzer"
LABEL="com.zhangyun.planet-stock-analyzer.zsxq-sync"
RUNTIME_DIR="/Users/zhangyun/Library/Application Support/planet-stock-analyzer-zsxq"
LOG_DIR="/Users/zhangyun/Library/Logs/planet-stock-analyzer"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/$LABEL.plist"

mkdir -p "$PLIST_DIR" "$RUNTIME_DIR/app" "$LOG_DIR"
cp "$PROJECT_DIR/scripts/sync_zsxq.py" "$RUNTIME_DIR/sync_zsxq.py"
cp "$PROJECT_DIR/scripts/run_zsxq_sync.sh" "$RUNTIME_DIR/run_zsxq_sync.sh"
cp "$PROJECT_DIR/app/zsxq.py" "$RUNTIME_DIR/app/zsxq.py"
cp "$PROJECT_DIR/app/__init__.py" "$RUNTIME_DIR/app/__init__.py"
chmod 700 "$RUNTIME_DIR/run_zsxq_sync.sh"
cp "$PROJECT_DIR/launchd/$LABEL.plist" "$PLIST_PATH"

cd /
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

printf '已安装并启动 %s\n运行副本：%s\n日志：%s/zsxq-sync.log\n' "$LABEL" "$RUNTIME_DIR" "$LOG_DIR"
