"""Environment status and setup, shared by the app and the command line.

Only the standard library is used, so this module runs on a bare system Python
before the virtualenv exists.  It is what the macOS app talks to when it needs to
know whether the editable environment is ready, or wants to build it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterator

TOOL = Path(__file__).resolve().parent.parent
VENV = TOOL / ".venv"
VENV_PYTHON = VENV / "bin" / "python"
REQUIREMENTS = TOOL / "requirements.txt"
AIRLIFT_REPO = "https://github.com/0xjohnnydev/airlift"
AIRLIFT = TOOL / "vendor" / "airlift"
DEVICE_HELPER = AIRLIFT / "build" / "device_helper"
XCODE = Path("/Applications/Xcode.app/Contents/Developer")

Write = Callable[[str], None]


def developer_dir() -> str:
    override = os.environ.get("DEVELOPER_DIR")
    if override:
        return override
    try:
        out = subprocess.run(["xcode-select", "-p"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, timeout=20)
        return out.stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def status() -> dict[str, object]:
    """Cheap checks; runs in well under a second."""
    dependencies = False
    if VENV_PYTHON.is_file():
        probe = subprocess.run([str(VENV_PYTHON), "-c", "import Crypto, pymobiledevice3"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        dependencies = probe.returncode == 0
    selected = developer_dir()
    return {
        "tool": str(TOOL),
        "checked": True,
        "venv": VENV_PYTHON.is_file(),
        "dependencies": dependencies,
        "airlift": DEVICE_HELPER.is_file(),
        "xcode": XCODE.is_dir() or "Xcode" in selected,
        "developerDir": selected,
        "python": sys.version.split()[0],
        "ready": bool(dependencies and DEVICE_HELPER.is_file()),
    }


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    if XCODE.is_dir():
        env["DEVELOPER_DIR"] = str(XCODE)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def run(command: list[str], write: Write | None = None) -> int:
    """Run a command, streaming its output line by line."""
    if write:
        write("$ " + " ".join(command) + "\n")
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, bufsize=1, env=child_env())
    assert process.stdout is not None
    for line in process.stdout:
        if write:
            write(line)
    return process.wait()


def install(write: Write) -> bool:
    """Create the virtualenv, install the requirements and build AirLift."""
    if not VENV_PYTHON.is_file():
        write("creating .venv\n")
        if run([sys.executable, "-m", "venv", str(VENV)], write) != 0:
            write("could not create the virtualenv\n")
            return False
    write("installing requirements\n")
    if run([str(VENV_PYTHON), "-m", "pip", "install", "--upgrade", "pip"], write) != 0:
        write("could not upgrade pip\n")
    if run([str(VENV_PYTHON), "-m", "pip", "install", "-r", str(REQUIREMENTS)], write) != 0:
        write("pip install failed - check the network connection\n")
        return False

    if not DEVICE_HELPER.is_file():
        if not AIRLIFT.is_dir():
            write("cloning AirLift\n")
            AIRLIFT.parent.mkdir(parents=True, exist_ok=True)
            if run(["git", "clone", "--depth", "1", AIRLIFT_REPO, str(AIRLIFT)], write) != 0:
                write("could not clone AirLift\n")
                return False
        write("building AirLift\n")
        if run(["make", "-C", str(AIRLIFT)], write) != 0:
            write("could not build AirLift - is a full Xcode installed?\n")
            return False
    write("environment ready\n")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m sksave.env",
                                     description="environment status and setup")
    parser.add_argument("--status", action="store_true", help="print the status as JSON")
    parser.add_argument("--install", action="store_true", help="install and build, streaming output")
    args = parser.parse_args(argv)
    if args.install:
        return 0 if install(lambda text: sys.stdout.write(text) or sys.stdout.flush()) else 1
    print(json.dumps(status(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
