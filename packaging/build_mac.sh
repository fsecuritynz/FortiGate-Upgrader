#!/bin/sh
# Build "FortiGate Upgrader.app" on macOS. Requires Python 3.11+.
#
# The app is built in a staging folder and swapped into dist/ in one step at the end, so a
# running copy is never left half-replaced. Quit and reopen the app after a rebuild.
#
# Code signing: macOS remembers "Always Allow" for the keychain per code signature.
# An ad-hoc signed build gets a new identity on every rebuild, so macOS asks once again
# after each build. Set SIGN_IDENTITY to a code-signing certificate in your keychain to keep
# the same identity across builds:
#   SIGN_IDENTITY="Developer ID Application: ..." packaging/build_mac.sh
set -e
cd "$(dirname "$0")/.."
if pgrep -f "FortiGate Upgrader.app/Contents/MacOS/FortiGate Upgrader" >/dev/null 2>&1; then
  echo "Note: FortiGate Upgrader is running. Quit and reopen it after this build to use the new version."
fi
python3 -m venv .build-venv
. .build-venv/bin/activate
pip install -q -r requirements.txt pyinstaller
STAGE="build/stage-dist"
rm -rf "$STAGE"
pyinstaller --noconfirm --clean --distpath "$STAGE" packaging/fortiupgrade.spec
NEW="$STAGE/FortiGate Upgrader.app"
if [ -n "$SIGN_IDENTITY" ]; then
  codesign --force --deep --options runtime --sign "$SIGN_IDENTITY" "$NEW"
fi
mkdir -p dist
APP="dist/FortiGate Upgrader.app"
if [ -d "$APP" ]; then mv "$APP" "dist/.old-app-$$"; fi
mv "$NEW" "$APP"
rm -rf "dist/.old-app-$$" "dist/FortiGate Upgrader" 2>/dev/null || true
if [ -n "$SIGN_IDENTITY" ]; then
  echo "Built and signed $APP with: $SIGN_IDENTITY"
else
  echo "Built $APP (ad-hoc signed: macOS asks for keychain access once after each rebuild;"
  echo "set SIGN_IDENTITY to keep approvals across builds, or choose 'Local key file' in Settings)."
fi
