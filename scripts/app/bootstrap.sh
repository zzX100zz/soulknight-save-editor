#!/bin/bash
# Launcher for the packaged .app.
#
# The bundle is only a carrier: the sources are copied to
# ~/Library/Application Support/SoulKnightSaveEditor/tool, where the virtualenv,
# the AirLift build, the pulled saves and the backups live.  Nothing inside the
# bundle is written to, so the app can be replaced without losing state.
#
# The web UI itself runs on the system Python (standard library only) so the page
# opens immediately and shows the dependency install / AirLift build as it runs.

set -uo pipefail

SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOME_DIR="$HOME/Library/Application Support/SoulKnightSaveEditor"
TOOL="$HOME_DIR/tool"

mkdir -p "$HOME_DIR"

if [ "$SOURCE" != "$TOOL" ]; then
    if ! command -v rsync >/dev/null 2>&1; then
        echo "rsync is missing; please run: xcode-select --install"
        read -r -p "press return to close" _
        exit 1
    fi
    rsync -a --delete --exclude '.venv' --exclude 'work' --exclude 'vendor' \
          --exclude '__pycache__' "$SOURCE/" "$TOOL/" || exit 1
fi

cd "$TOOL"
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)'; then
    echo "python3 is required (install the command line tools: xcode-select --install)"
    read -r -p "press return to close" _
    exit 1
fi

echo "starting the web UI - the browser will open at http://127.0.0.1:8787/"
echo "keep this window open while you use it; press Ctrl+C to stop."
echo
exec python3 -m sksave.web --open --port 8787
