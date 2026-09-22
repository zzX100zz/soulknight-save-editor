#!/bin/bash
# Launcher for the packaged .app.
#
# The app bundle is only a carrier: on first launch the sources are copied to
# ~/Library/Application Support/SoulKnightSaveEditor/tool, where the virtualenv
# and AirLift live too.  Nothing inside the bundle is ever written to, so the app
# can be replaced or reinstalled without losing the build or the backups.
#
# All output is visible in the Terminal window the launcher opened.

set -uo pipefail

SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOME_DIR="$HOME/Library/Application Support/SoulKnightSaveEditor"
TOOL="$HOME_DIR/tool"
VENV="$HOME_DIR/venv"
WORK="$HOME_DIR/work"

echo "Soul Knight save editor"
echo "  sources: $SOURCE"
echo "  home:    $HOME_DIR"
echo

mkdir -p "$HOME_DIR" "$WORK"

# 1. copy (or refresh) the tool next to the state it needs
if [ "$SOURCE" != "$TOOL" ]; then
    if ! command -v rsync >/dev/null 2>&1; then
        echo "rsync is missing; please run: xcode-select --install"
        exit 1
    fi
    rsync -a --delete --exclude '.venv' --exclude 'work' --exclude 'vendor' \
          --exclude '__pycache__' "$SOURCE/" "$TOOL/" || exit 1
fi

# 2. dependencies
deps_ok() {
    [ -x "$VENV/bin/python" ] &&
        "$VENV/bin/python" - <<'PY' >/dev/null 2>&1
import Crypto, pymobiledevice3  # noqa: F401
PY
}

if deps_ok; then
    echo "dependencies: ok"
else
    echo "dependencies: installing (a minute or two the first time)"
    rm -rf "$VENV"
    python3 -m venv "$VENV" || { echo "could not create the virtualenv (need python3)"; exit 1; }
    "$VENV/bin/python" -m pip install --upgrade pip
    if ! "$VENV/bin/python" -m pip install -r "$TOOL/requirements.txt"; then
        echo
        echo "pip failed - check the network connection and run the app again."
        exit 1
    fi
    deps_ok || { echo "dependencies still incomplete"; exit 1; }
fi

# 3. AirLift (cloned and built here, never redistributed)
if [ ! -x "$TOOL/vendor/airlift/build/device_helper" ]; then
    echo "AirLift: cloning and building (needs a full Xcode 27 installation)"
    if ! (cd "$TOOL" && "$VENV/bin/python" -c "from sksave import device; device.ensure_airlift()"); then
        echo
        echo "Building AirLift failed. Check that Xcode 27 is installed:"
        echo "  xcode-select -p     # should print /Applications/Xcode.app/Contents/Developer"
        exit 1
    fi
fi
echo "AirLift: ok"

# 4. run
echo
cd "$TOOL"
"$VENV/bin/python" run.py --workdir "$WORK"
status=$?
echo
[ $status -ne 0 ] && echo "exited with status $status - the output above has the details."
echo "You can close this window."
