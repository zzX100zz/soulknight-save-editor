#!/usr/bin/env bash
# Build a double-clickable .app and pack it into a DMG for releases.
#
# The bundle carries the whole tool (sources + launcher).  On first launch it
# creates its own virtualenv next to the sources and installs requirements.txt,
# then opens the interactive menu in Terminal.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME="SoulKnightSaveEditor"
VERSION="${VERSION:-$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/sksave/__init__.py" | head -1)}"
BUILD="$ROOT/build"
APP="$BUILD/$NAME.app"
DMG="$ROOT/dist/$NAME-$VERSION.dmg"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" "$ROOT/dist"

cp "$ROOT/scripts/app/Info.plist" "$APP/Contents/Info.plist"
sed -i '' "s/__VERSION__/$VERSION/" "$APP/Contents/Info.plist"
cp "$ROOT/scripts/app/launcher" "$APP/Contents/MacOS/$NAME"
chmod +x "$APP/Contents/MacOS/$NAME"
cp "$ROOT/scripts/app/使用说明.txt" "$APP/Contents/Resources/使用说明.txt"

# ship the sources, not the local state
rsync -a --exclude '.venv' --exclude 'work' --exclude 'build' --exclude 'dist' \
      --exclude 'vendor' --exclude '__pycache__' --exclude '.git' \
      "$ROOT/" "$APP/Contents/Resources/tool/"

# ad-hoc signature: keeps the bundle stable and avoids translocation surprises
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || echo "warning: ad-hoc signing failed"

echo "packing $DMG"
rm -f "$DMG"
STAGE="$BUILD/dmg"
rm -rf "$STAGE"; mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
cp "$ROOT/scripts/app/使用说明.txt" "$STAGE/使用说明.txt"
ln -s /Applications "$STAGE/Applications"

hdiutil create -volname "$NAME $VERSION" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
echo "built: $DMG"
