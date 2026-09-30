#!/bin/bash
# Install PhoneDrive: venv + a login LaunchAgent that watches for the phone.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL=com.phonedrive.watcher
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

[ -x "$DIR/.venv/bin/python" ] || python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --disable-pip-version-check -r "$DIR/requirements.txt"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
sed -e "s|__PROJECT__|$DIR|g" -e "s|__HOME__|$HOME|g" "$DIR/launchd/$LABEL.plist" > "$PLIST"
plutil -lint "$PLIST" > /dev/null

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "PhoneDrive installed. Plug in your phone. Log: ~/Library/Logs/PhoneDrive.log"
