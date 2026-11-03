#!/bin/zsh
# Builds Дежурный.app into /Applications. Needs only the Command Line Tools (swiftc).
set -e
cd "${0:A:h}"
B=$(mktemp -d "${TMPDIR:-/tmp}/dezhurny-build.XXXXXX")
trap 'rm -rf "$B"' EXIT
APP="$B/Дежурный.app"
SCRIPT="${0:A:h:h}/codex_wake.py"
PY="$(command -v python3)"
mkdir -p "$APP/Contents/MacOS"
swiftc -O -target "$(uname -m)-apple-macosx14.0" -swift-version 5 -parse-as-library -o "$APP/Contents/MacOS/Dezhurny" Dezhurny.swift
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Дежурный</string>
  <key>CFBundleIdentifier</key><string>local.damir.dezhurny</string>
  <key>CFBundleExecutable</key><string>Dezhurny</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
  <key>DutyScript</key><string>$SCRIPT</string>
  <key>DutyPython</key><string>$PY</string>
</dict></plist>
PLIST
codesign --force --deep -s - "$APP"
pkill -x Dezhurny 2>/dev/null || true
rm -rf "/Applications/Дежурный.app"
cp -R "$APP" "/Applications/Дежурный.app"
echo "installed: /Applications/Дежурный.app"
