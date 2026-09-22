"""Local web UI for the save editor.

This module deliberately uses nothing outside the standard library: the page is
served by a plain ``http.server`` so it can start on the system Python before the
virtualenv exists.  Every operation that needs the save crypto (and therefore
third-party modules) is executed as a subprocess with the project's virtualenv,
and its output is streamed into the browser, which is also how the dependency
install and the AirLift build report their progress.

    python3 -m sksave.web --open        # opens http://127.0.0.1:8787

The server binds to the loopback interface only, and state-changing requests
must carry the ``X-Requested-With: sksave`` header, so a random web page cannot
drive it.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

TOOL = Path(__file__).resolve().parent.parent
VENV_PYTHON = TOOL / ".venv" / "bin" / "python"
VENDOR_DEVICE_HELPER = TOOL / "vendor" / "airlift" / "build" / "device_helper"
XCODE = Path("/Applications/Xcode.app/Contents/Developer")
UI_FILE = Path(__file__).resolve().parent / "webui.html"
DEFAULT_PORT = 8787

# the toggles the UI offers, in the order they should appear
OPTION_NAMES = (
    "heroes", "hero_levels", "skins", "pets", "skills", "weapons", "weapon_skins",
    "evolution", "kill_effects", "mythic", "materials", "season_coin", "gems",
    "repair_format",
)


# --------------------------------------------------------------------------- #
# environment
# --------------------------------------------------------------------------- #
def _run(command: list[str], *, timeout: int = 60) -> tuple[int, str]:
    completed = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout,
    )
    return completed.returncode, completed.stdout or ""


def environment() -> dict[str, Any]:
    venv = VENV_PYTHON.is_file()
    dependencies = False
    if venv:
        code, _ = _run([str(VENV_PYTHON), "-c", "import Crypto, pymobiledevice3"], timeout=60)
        dependencies = code == 0
    return {
        "tool": str(TOOL),
        "venv": venv,
        "dependencies": dependencies,
        "airlift": VENDOR_DEVICE_HELPER.is_file(),
        "xcode": XCODE.is_dir(),
        "xcodeSelect": _developer_dir(),
        "python": sys.version.split()[0],
    }


def _developer_dir() -> str:
    try:
        code, out = _run(["xcode-select", "-p"], timeout=20)
        return out.strip() if code == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    if XCODE.is_dir():
        env["DEVELOPER_DIR"] = str(XCODE)
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #
class Job:
    def __init__(self, name: str) -> None:
        self.id = int(time.time() * 1000)
        self.name = name
        self.status = "running"
        self.log: list[str] = []
        self.started = time.time()
        self.finished: float | None = None
        self.result: dict[str, Any] = {}

    def write(self, text: str) -> None:
        for line in text.splitlines(keepends=True):
            self.log.append(line)

    def snapshot(self, cursor: int) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "cursor": len(self.log),
            "log": "".join(self.log[max(0, cursor):]),
            "seconds": round((self.finished or time.time()) - self.started, 1),
            "result": self.result,
        }


class Registry:
    """One job at a time, with a bounded in-memory log."""

    def __init__(self) -> None:
        self.current: Job | None = None
        self.history: deque[Job] = deque(maxlen=20)
        self.state: dict[str, Any] | None = None
        self.lock = threading.Lock()

    def start(self, name: str, task: Callable[[Job], dict[str, Any]]) -> Job:
        with self.lock:
            if self.current and self.current.status == "running":
                raise RuntimeError(f"a job is already running ({self.current.name})")
            job = Job(name)
            self.current = job
            self.history.append(job)

        def runner() -> None:
            try:
                job.result = task(job) or {}
                if "state" in job.result:
                    self.state = job.result["state"]
                job.status = "done"
            except Exception as error:  # noqa: BLE001
                job.write(f"\nerror: {error}\n")
                job.status = "failed"
                job.result = {"error": str(error)}
            finally:
                job.finished = time.time()

        threading.Thread(target=runner, daemon=True).start()
        return job

    def latest(self) -> dict[str, Any]:
        if not self.current:
            return {"status": "idle", "name": "", "cursor": 0, "log": "", "result": {}}
        return self.current.snapshot(0)


REGISTRY = Registry()
_state_lock = threading.Lock()


def stream(command: list[str], job: Job) -> int:
    """Run a child process and stream its output into the job log."""
    job.write("$ " + " ".join(shlex_quote(part) for part in command) + "\n")
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        bufsize=1, env=child_env(),
    )
    assert process.stdout is not None
    for line in process.stdout:
        job.write(line)
    return process.wait()


def shlex_quote(value: str) -> str:
    return value if all(character not in value for character in " \t\"'\\$") else repr(value)


# --------------------------------------------------------------------------- #
# job implementations
# --------------------------------------------------------------------------- #
def job_setup(job: Job) -> dict[str, Any]:
    if not VENV_PYTHON.is_file():
        job.write("creating .venv\n")
        code = stream([sys.executable, "-m", "venv", str(TOOL / ".venv")], job)
        if code != 0:
            raise RuntimeError("could not create the virtualenv")
    job.write("installing requirements\n")
    code = stream([str(VENV_PYTHON), "-m", "pip", "install", "--upgrade", "pip"], job)
    code = stream([str(VENV_PYTHON), "-m", "pip", "install", "-r", str(TOOL / "requirements.txt")], job)
    if code != 0:
        raise RuntimeError("pip install failed")

    if not VENDOR_DEVICE_HELPER.is_file():
        job.write("cloning and building AirLift\n")
        code = stream(
            ["git", "clone", "--depth", "1", "https://github.com/0xjohnnydev/airlift",
             str(TOOL / "vendor" / "airlift")],
            job,
        )
        if code != 0:
            raise RuntimeError("could not clone AirLift")
        code = stream(["make", "-C", str(TOOL / "vendor" / "airlift")], job)
        if code != 0:
            raise RuntimeError("could not build AirLift - is Xcode 27 installed?")
    job.write("\nenvironment ready\n")
    return {"environment": environment()}


def _cli(job: Job, arguments: list[str], *, report: bool = True) -> dict[str, Any]:
    """Run the CLI in the virtualenv, optionally reading a JSON report back."""
    if not VENV_PYTHON.is_file():
        raise RuntimeError("the environment is not installed yet - press 'install' first")
    report_path = TOOL / "work" / "web-report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.unlink(missing_ok=True)
    command = [str(VENV_PYTHON), str(TOOL / "run.py")]
    if report:                                     # global options go before the subcommand
        command += ["--report", str(report_path)]
    command += arguments
    code = stream(command, job)
    result: dict[str, Any] = {"exitCode": code}
    if report and report_path.is_file():
        try:
            payload = json.loads(report_path.read_text())
            result.update(payload if isinstance(payload, dict) else {"report": payload})
        except json.JSONDecodeError:
            pass
    if code != 0:
        raise RuntimeError(f"the command exited with status {code}")
    return result


def job_devices(job: Job) -> dict[str, Any]:
    code, out = _run([str(VENV_PYTHON), str(TOOL / "run.py"), "devices"], timeout=120)
    job.write(out)
    return {"exitCode": code}


def job_info(job: Job) -> dict[str, Any]:
    return _cli(job, ["info"])


def job_backup(job: Job, label: str = "ui") -> dict[str, Any]:
    return _cli(job, ["backup", "--label", label])


def job_unlock(job: Job, payload: dict[str, Any]) -> dict[str, Any]:
    arguments = ["unlock"]
    for name in OPTION_NAMES:
        if payload.get("options", {}).get(name) is False:
            arguments.append("--no-" + name.replace("_", "-"))
    for key, flag in (("gems", "--gems"), ("seasonCoin", "--season-coin"),
                      ("quantity", "--quantity"), ("heroLevel", "--hero-level")):
        value = payload.get(key)
        if isinstance(value, int) and value > 0:
            arguments += [flag, str(value)]
    if payload.get("label"):
        arguments += ["--label", str(payload["label"])]
    return _cli(job, arguments)


def job_restore(job: Job, payload: dict[str, Any]) -> dict[str, Any]:
    source = str(payload.get("source") or "")
    if not source:
        raise RuntimeError("no backup selected")
    if payload.get("kind") == "device":
        return _cli(job, ["restore", "--from-device", source])
    return _cli(job, ["restore", "--from", source])


def _read_state(report: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("state", "after"):
        value = report.get(key)
        if isinstance(value, dict):
            return value
    return None


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "sksave"

    def log_message(self, *args: Any) -> None:  # keep the console quiet
        pass

    # -- helpers ---------------------------------------------------------- #
    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body: str) -> None:
        blob = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(blob)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            return payload if isinstance(payload, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _local_only(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in {"127.0.0.1", "localhost", "[::1]", ""}

    # -- routes ----------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802
        if not self._local_only():
            self._json({"error": "local access only"}, 403)
            return
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        route = parsed.path

        if route in ("/", "/index.html"):
            self._html(UI_FILE.read_text(encoding="utf-8"))
        elif route == "/api/status":
            self._json(status_payload())
        elif route == "/api/state":
            self._json({"state": REGISTRY.state})
        elif route == "/api/backups":
            self._json(backups_payload())
        elif route == "/api/job":
            job = REGISTRY.current
            if job and query.get("id", [str(job.id)])[0] == str(job.id):
                self._json(job.snapshot(int(query.get("cursor", ["0"])[0] or 0)))
            else:
                self._json(REGISTRY.latest())
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        if not self._local_only() or self.headers.get("X-Requested-With") != "sksave":
            self._json({"error": "rejected"}, 403)
            return
        payload = self._body()
        name = str(payload.get("name") or "")
        try:
            if name == "setup":
                job = REGISTRY.start("setup", job_setup)
            elif name == "devices":
                job = REGISTRY.start("devices", job_devices)
            elif name == "info":
                job = REGISTRY.start("info", lambda job: job_info(job))
            elif name == "backup":
                job = REGISTRY.start("backup", lambda job: job_backup(job, payload.get("label") or "ui"))
            elif name == "unlock":
                job = REGISTRY.start("unlock", lambda job: job_unlock(job, payload))
            elif name == "restore":
                job = REGISTRY.start("restore", lambda job: job_restore(job, payload))
            else:
                self._json({"error": f"unknown job {name!r}"}, 400)
                return
        except RuntimeError as error:
            self._json({"error": str(error)}, 409)
            return
        self._json({"id": job.id, "name": job.name, "status": job.status})

    # keep the browser from caching the single page
    def do_HEAD(self) -> None:  # noqa: N802
        self.send_response(200)
        self.end_headers()


# --------------------------------------------------------------------------- #
# status helpers (cheap, no device access)
# --------------------------------------------------------------------------- #
_devices_cache: dict[str, Any] = {"at": 0.0, "devices": []}


def devices() -> list[dict[str, Any]]:
    if time.time() - _devices_cache["at"] > 10:
        entries: list[dict[str, Any]] = []
        try:
            entries = _cli_devices()
        except Exception:  # noqa: BLE001
            entries = []
        _devices_cache.update({"at": time.time(), "devices": entries})
    return _devices_cache["devices"]


def _cli_devices() -> list[dict[str, Any]]:
    if not VENV_PYTHON.is_file():
        return []
    code, out = _run(
        [str(VENV_PYTHON), "-c",
         "import json,sys;sys.path.insert(0,%r);from sksave import device;"
         "print(json.dumps(device.list_devices()))" % str(TOOL)],
        timeout=60,
    )
    if code != 0:
        return []
    line = out.strip().splitlines()[-1] if out.strip() else "[]"
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return []
    return payload if isinstance(payload, list) else []


_backups_cache: dict[str, Any] = {"at": 0.0, "payload": {"local": [], "device": []}}


def backups_payload() -> dict[str, Any]:
    """Ask the CLI for both backup sets; cached briefly because it touches the phone."""
    local: list[str] = []
    backups_dir = TOOL / "work" / "backups"
    if backups_dir.is_dir():
        local = sorted(str(path) for path in backups_dir.iterdir() if path.is_dir())
    if time.time() - _backups_cache["at"] < 15 and _backups_cache["payload"]["local"] == local:
        return _backups_cache["payload"]

    device: list[str] = []
    if VENV_PYTHON.is_file():
        report_path = TOOL / "work" / "web-restore-list.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.unlink(missing_ok=True)
        command = [str(VENV_PYTHON), str(TOOL / "run.py"), "--report", str(report_path),
                   "restore", "--list"]
        try:
            code, _ = _run(command, timeout=180)
            if code == 0 and report_path.is_file():
                payload = json.loads(report_path.read_text())
                device = sorted(str(path) for path in payload.get("device", []))
        except Exception:  # noqa: BLE001
            device = []
    payload = {"local": local, "device": device}
    _backups_cache.update({"at": time.time(), "payload": payload})
    return payload


def status_payload() -> dict[str, Any]:
    return {
        "environment": environment(),
        "devices": devices(),
        "options": list(OPTION_NAMES),
        "job": REGISTRY.latest(),
        "state": REGISTRY.state,
        "version": _version(),
    }


def _version() -> str:
    for line in (TOOL / "sksave" / "__init__.py").read_text().splitlines():
        if line.startswith("__version__"):
            return line.split("=")[1].strip().strip('"')
    return ""


# --------------------------------------------------------------------------- #
def serve(port: int = DEFAULT_PORT, *, open_browser: bool = False, tries: int = 12) -> None:
    for offset in range(tries):
        candidate = port + offset
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
        except OSError:
            continue
        url = f"http://127.0.0.1:{candidate}/"
        print(f"soulknight-save-editor web UI: {url}")
        print("press Ctrl+C to stop")
        if open_browser:
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
        finally:
            httpd.server_close()
        return
    raise RuntimeError(f"no free port between {port} and {port + tries - 1}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m sksave.web", description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open", action="store_true", help="open the browser")
    parser.add_argument("--setup", action="store_true", help="install the environment and exit")
    args = parser.parse_args(argv)

    if args.setup:
        job = Job("setup")
        payload = job_setup(job)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload else 1

    serve(args.port, open_browser=True if args.open else False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
