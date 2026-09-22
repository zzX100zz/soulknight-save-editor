#!/bin/bash
# Build SoulKnightSaveEditor.app: a normal macOS application bundle.
#
# The executable is a compiled AppKit program (scripts/app/SoulKnightSaveEditor.swift)
# that hosts the interface in a window and runs the Python command line as child
# processes.  The Python sources ship inside the bundle and are copied to
# ~/Library/Application Support/SoulKnightSaveEditor/tool on first launch.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${VERSION:-$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/sksave/__init__.py" | head -1)}"
NAME="SoulKnightSaveEditor"
BUILD="$ROOT/build"
APP="$ROOT/dist/$NAME.app"

echo "building $NAME.app $VERSION"
rm -rf "$APP" "$BUILD/app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/tool"

# 1. native executable
xcrun swiftc -O -swift-version 5 \
    -target "$(uname -m)-apple-macosx13.0" \
    -framework AppKit -framework WebKit \
    -o "$APP/Contents/MacOS/$NAME" \
    "$ROOT/scripts/app/$NAME.swift"

# 2. Info.plist, icon
sed "s/__VERSION__/$VERSION/" "$ROOT/scripts/app/Info.plist" > "$APP/Contents/Info.plist"
if [ -f "$ROOT/scripts/app/AppIcon.icns" ]; then
    cp "$ROOT/scripts/app/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
fi

# 3. python sources (no virtualenv, no pulled saves, no AirLift checkout)
rsync -a --exclude '.venv' --exclude 'work' --exclude 'vendor' --exclude 'build' \
      --exclude 'dist' --exclude '__pycache__' --exclude '.git' --exclude '.pytest_cache' \
      --exclude '*.data' --exclude 'backups' \
      "$ROOT/run.py" "$ROOT/sksave" "$ROOT/requirements.txt" "$ROOT/README.md" \
      "$ROOT/LICENSE" "$ROOT/Makefile" "$ROOT/docs" "$ROOT/tests" "$ROOT/scripts" \
      "$APP/Contents/Resources/tool/"

# 4. ad-hoc signature so the bundle is stable and the icon shows up
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || echo "warning: ad-hoc signing failed"

echo "built: $APP"
