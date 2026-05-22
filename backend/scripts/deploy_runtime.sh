#!/bin/zsh
set -euo pipefail

SOURCE_BACKEND="/Users/applemima111/Desktop/动画/bicaraifilm/bicar_ai_storyboard/backend"
RUNTIME_BACKEND="/Users/applemima111/bicar_runtime/bicar_ai_storyboard/backend"
API_PLIST="$HOME/Library/LaunchAgents/com.bicar.storyboard.api.plist"
WS_PLIST="$HOME/Library/LaunchAgents/com.bicar.storyboard.feishu_ws.plist"
UID_NUM="$(id -u)"

mkdir -p "$RUNTIME_BACKEND"
rsync -a --delete \
  --exclude 'logs/' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  "$SOURCE_BACKEND/" \
  "$RUNTIME_BACKEND/"
mkdir -p "$RUNTIME_BACKEND/logs"

launchctl bootout "gui/${UID_NUM}" "$API_PLIST" >/dev/null 2>&1 || true
launchctl bootout "gui/${UID_NUM}" "$WS_PLIST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/${UID_NUM}" "$API_PLIST"
launchctl bootstrap "gui/${UID_NUM}" "$WS_PLIST"
launchctl enable "gui/${UID_NUM}/com.bicar.storyboard.api"
launchctl enable "gui/${UID_NUM}/com.bicar.storyboard.feishu_ws"
launchctl kickstart -k "gui/${UID_NUM}/com.bicar.storyboard.api"
launchctl kickstart -k "gui/${UID_NUM}/com.bicar.storyboard.feishu_ws"

echo "Runtime deployed and services restarted."
