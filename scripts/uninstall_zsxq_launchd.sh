#!/bin/sh

set -eu

LABEL="com.zhangyun.planet-stock-analyzer.zsxq-sync"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"

cd /
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$PLIST_PATH"
printf '已停止并移除 %s\n' "$LABEL"
