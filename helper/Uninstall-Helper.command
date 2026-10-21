#!/bin/bash
# Takes the C3 Life helper off this Mac: stops it and removes its login item. Its key is kept unless you say so,
# so installing it again later doesn't need the key typed on the C3 Life server again.
#   Double-click it, or:  bash Uninstall-Helper.command [--yes] [--remove-key]

set -uo pipefail
DIR="$HOME/Library/Application Support/C3 Life Helper"
PLIST="$HOME/Library/LaunchAgents/org.c3life.helper.plist"
LABEL="org.c3life.helper"
YES=0; KEY=0
for a in "$@"; do [ "$a" = "--yes" ] && YES=1; [ "$a" = "--remove-key" ] && KEY=1; done

if [ "$YES" != 1 ]; then
  read -r -p "Stop the C3 Life helper and stop it starting at login? [y/N] " ans || ans=""
  case "$ans" in [yY]*) ;; *) echo "Nothing changed."; exit 0;; esac
fi
launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || launchctl unload "$PLIST" >/dev/null 2>&1 || true
rm -f "$PLIST" "$DIR/c3life_helper.py" "$DIR/helper.log"
if [ "$KEY" = 1 ]; then rm -rf "$DIR"; echo "Removed, with its key."; else echo "Removed. Its key is kept in $DIR/helper.json for next time."; fi
[ "$YES" = 1 ] || { read -r -p "Press Return to close this window." _ || true; }
