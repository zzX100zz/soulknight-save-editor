#!/bin/bash
# Package the built .app into a distributable DMG.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${VERSION:-$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/sksave/__init__.py" | head -1)}"
NAME="SoulKnightSaveEditor"
APP="$ROOT/dist/$NAME.app"
DMG="$ROOT/dist/$NAME-$VERSION.dmg"

bash "$ROOT/scripts/build_app.sh"

echo "packing $DMG"
STAGE="$ROOT/build/dmg"
rm -rf "$STAGE"; mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
cp "$ROOT/scripts/app/使用说明.txt" "$STAGE/使用说明.txt"
ln -s /Applications "$STAGE/Applications"

rm -f "$DMG"
hdiutil create -volname "$NAME $VERSION" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
echo "built: $DMG"
