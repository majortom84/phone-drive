#!/bin/bash
# Remove PhoneDrive's LaunchAgent and unmount any phone drive.
set -uo pipefail
LABEL=com.phonedrive.watcher
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
pkill -TERM -f "phone_drive.mount" 2>/dev/null
sleep 2
mount | sed -n 's/^phonedrive on \(.*\) (macfuse.*/\1/p' | while read -r mp; do umount -f "$mp"; done
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "PhoneDrive removed. (The project folder, macFUSE and libmtp are left in place.)"
