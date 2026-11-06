#!/bin/sh
# Package dist/FortiGate Upgrader.app as a drag-to-Applications disk image:
#   dist/FortiGate-Upgrader-<version>-macOS-<arch>.dmg
# Run after packaging/build_mac.sh. The in-app updater looks for this file name on GitHub Releases.
set -e
cd "$(dirname "$0")/.."
APP="dist/FortiGate Upgrader.app"
[ -d "$APP" ] || { echo "build the app first: packaging/build_mac.sh"; exit 1; }
VER=$(python3 -c 'import re;print(re.search(r"__version__ = \"([^\"]+)\"", open("fortiupgrade/__init__.py").read()).group(1))')
ARCH=$(lipo -archs "$APP/Contents/MacOS/FortiGate Upgrader" 2>/dev/null | tr ' ' '\n' | head -1)
case "$ARCH" in arm64) ARCH=arm64 ;; *) ARCH=x86_64 ;; esac
OUT="dist/FortiGate-Upgrader-$VER-macOS-$ARCH.dmg"
STAGE=$(mktemp -d)
ditto "$APP" "$STAGE/FortiGate Upgrader.app"
ln -s /Applications "$STAGE/Applications"
rm -f "$OUT"
hdiutil create -quiet -volname "FortiGate Upgrader $VER" -srcfolder "$STAGE" -fs HFS+ -format UDZO -ov "$OUT"
rm -rf "$STAGE"
echo "Built $OUT"
