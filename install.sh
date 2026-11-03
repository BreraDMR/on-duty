#!/bin/zsh
# Installs the daemon (launchd agent) and the menu bar app for the current user.
# Re-run it after moving the folder. Uninstall: ./install.sh --uninstall
set -e
DIR="${0:A:h}"
PY="$(command -v python3)"
AGENTS="$HOME/Library/LaunchAgents"
WATCH=local.on-duty.watch
MENU=local.on-duty.menubar
DOM="gui/$(id -u)"

launchctl bootout "$DOM/$WATCH" 2>/dev/null || true
launchctl bootout "$DOM/$MENU" 2>/dev/null || true
if [[ "$1" == "--uninstall" ]]; then
  rm -f "$AGENTS/$WATCH.plist" "$AGENTS/$MENU.plist"
  pkill -x Dezhurny 2>/dev/null || true
  rm -rf "/Applications/Дежурный.app"
  echo "uninstalled (state/ in $DIR is kept)"
  exit 0
fi

mkdir -p "$AGENTS" "$DIR/state"
"$DIR/menubar/build.sh"

cat > "$AGENTS/$WATCH.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$WATCH</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$DIR/codex_wake.py</string><string>daemon</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>$DIR/state/daemon.stderr.log</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>${PY:h}:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
</dict></plist>
PLIST

cat > "$AGENTS/$MENU.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$MENU</string>
  <key>ProgramArguments</key><array><string>/usr/bin/open</string><string>-a</string><string>/Applications/Дежурный.app</string></array>
  <key>RunAtLoad</key><true/>
</dict></plist>
PLIST

launchctl enable "$DOM/$WATCH"
launchctl bootstrap "$DOM" "$AGENTS/$WATCH.plist"
launchctl bootstrap "$DOM" "$AGENTS/$MENU.plist"
echo "installed: daemon $WATCH, menu bar app /Applications/Дежурный.app"
