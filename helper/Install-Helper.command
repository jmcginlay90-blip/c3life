#!/bin/bash
# Installs the C3 Life helper on this Mac, so it starts by itself whenever someone logs in, and restarts if it stops.
# No Terminal window to keep open. Run it again to update the helper (the key stays the same).
#
#   Double-click it in Finder. If macOS says it can't be opened: right-click it > Open > Open.
#   Or in Terminal:  bash Install-Helper.command          (add --yes to skip the question)
#
# It copies c3life_helper.py to ~/Library/Application Support/C3 Life Helper, adds a login item
# (~/Library/LaunchAgents/org.c3life.helper.plist), starts it, and shows its key once.
# To take it off again: Uninstall-Helper.command

set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DIR="$HOME/Library/Application Support/C3 Life Helper"
PLIST="$HOME/Library/LaunchAgents/org.c3life.helper.plist"
LABEL="org.c3life.helper"
YES=0
[ "${1:-}" = "--yes" ] && YES=1

say() { printf '%s\n' "$*"; }
done_wait() { [ "$YES" = 1 ] || { say ""; read -r -p "Press Return to close this window." _ || true; }; }
trap 'say ""; say "Something went wrong (line $LINENO). Nothing else was changed."; done_wait' ERR

say "C3 Life helper for this Mac"
say "It lets the C3 Life server play Spotify here (from the Music screen) and open ProPresenter."
say ""

[ -f "$HERE/c3life_helper.py" ] || { say "c3life_helper.py isn't next to this file. Run it from the helper folder."; done_wait; exit 1; }

# Python 3: macOS has it once the Command Line Tools are installed (typing python3 offers to install them).
PY=""
for p in /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if [ -x "$p" ] && "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then PY="$p"; break; fi
done
if [ -z "$PY" ]; then
  say "Python 3 isn't installed yet. macOS will offer to install it (the Command Line Tools) now."
  say "Click Install, wait for it to finish, then run this again."
  /usr/bin/python3 --version >/dev/null 2>&1 || true
  done_wait; exit 1
fi

if [ "$YES" != 1 ]; then
  read -r -p "Install the C3 Life helper so it starts by itself at login? [Y/n] " ans || ans=""
  case "$ans" in [nN]*) say "Nothing changed."; done_wait; exit 0;; esac
fi

mkdir -p "$DIR" "$HOME/Library/LaunchAgents"
cp "$HERE/c3life_helper.py" "$DIR/c3life_helper.py"
# The key and id stay the same when it's installed again (they're in helper.json, which isn't replaced).
KEY="$("$PY" "$DIR/c3life_helper.py" --config "$DIR/helper.json" --show-key)"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$DIR/c3life_helper.py</string>
    <string>--config</string>
    <string>$DIR/helper.json</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$DIR/helper.log</string>
  <key>StandardErrorPath</key><string>$DIR/helper.log</string>
</dict>
</plist>
EOF

# (Re)start it now, without waiting for the next login.
launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || launchctl unload "$PLIST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load -w "$PLIST"

ok=0
for _ in $(seq 1 20); do
  if curl -s -m 2 http://127.0.0.1:8099/hello | grep -q c3life-helper; then ok=1; break; fi
  sleep 0.5
done

say ""
if [ "$ok" = 1 ]; then
  say "Done. The C3 Life helper is running, and starts by itself whenever someone logs in to this Mac."
else
  say "Installed, but it isn't answering yet. Look in: $DIR/helper.log"
fi
say ""
say "Its key (an Admin types it once on the C3 Life server: Show > Music > Computers with the C3 Life helper):"
say ""
say "    $KEY"
say ""
say "The C3 Life server finds this Mac by itself within 5 minutes (or tap Find it now on Music)."
say "The first time Music plays Spotify here, the Mac asks whether Python may control Spotify: click OK."
say "Keep this Mac logged in (System Settings > Users & Groups > Automatically log in) so the helper is always there."
trap - ERR
done_wait
