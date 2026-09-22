"""The device flow has to survive a stale container path and report it clearly."""

from __future__ import annotations

import json

import pytest

from sksave import env as env_module
from sksave import session as session_module
from sksave.device import SkError


@pytest.fixture()
def fake_device(tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(session_module.time, "sleep", lambda seconds: None)   # no backoff in tests

    def fake_pull(udid, remote, local):
        calls.append(remote)
        if remote.startswith("/old/"):
            raise RuntimeError("pull failed: /old/Documents did not reach Media")
        from pathlib import Path

        Path(local).mkdir(parents=True, exist_ok=True)
        (Path(local) / "game.data").write_bytes(b"x")

    monkeypatch.setattr(session_module.dev, "pull", fake_pull)
    device = session_module.Device(udid="udid", name="iPhone", version="27.0", container="/old")
    work = tmp_path / "work"
    work.mkdir()
    return session_module.SaveSession(device, work, log=lambda message: None), calls


def test_pull_recovers_when_the_container_moved(fake_device, monkeypatch):
    session, calls = fake_device
    monkeypatch.setattr(session_module.dev, "find_container", lambda *a, **k: "/new")
    session.pull(with_prefs=False)
    assert calls[0].startswith("/old/")           # the remembered path was tried first
    assert calls[-1].startswith("/new/")          # and the rediscovered one finished the job
    assert session.device.container == "/new"
    assert (session.live / "Documents" / "game.data").is_file()


def test_pull_explains_a_locked_phone(fake_device, monkeypatch):
    session, _ = fake_device
    monkeypatch.setattr(session_module.dev, "find_container", lambda *a, **k: None)
    with pytest.raises(SkError) as error:
        session.pull(with_prefs=False)
    message = str(error.value)
    assert "Unlock the iPhone" in message
    assert "--container" in message


def test_environment_status_is_json_ready(monkeypatch, capsys):
    monkeypatch.setattr(env_module, "VENV_PYTHON", env_module.Path("/nonexistent/python"))
    monkeypatch.setattr(env_module, "DEVICE_HELPER", env_module.Path("/nonexistent/device_helper"))
    monkeypatch.setattr(env_module, "XCODE", env_module.Path("/nonexistent/Xcode.app"))
    assert env_module.main([]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["venv"] is False
    assert payload["dependencies"] is False
    assert payload["ready"] is False
    # the app only greys its buttons when the check actually ran and said no
    assert payload["checked"] is True
    assert "tool" in payload and "python" in payload

@pytest.fixture()
def working_session(tmp_path, monkeypatch):
    """A session whose container answers, so the marker logic is what is under test."""
    monkeypatch.setattr(session_module.time, "sleep", lambda seconds: None)
    pushed: list[str] = []

    def fake_pull(udid, remote, local):
        from pathlib import Path

        Path(local).mkdir(parents=True, exist_ok=True)
        if remote.endswith("Documents"):
            (Path(local) / "game.data").write_bytes(b"x")

    def fake_push_tree(udid, local, remote):
        pushed.append(str(local))

    monkeypatch.setattr(session_module.dev, "pull", fake_pull)
    monkeypatch.setattr(session_module.dev, "push", lambda *a, **k: {})
    monkeypatch.setattr(session_module.dev, "push_tree", fake_push_tree)
    device = session_module.Device(udid="udid", name="iPhone", version="27.0", container="/c")
    work = tmp_path / "work"
    work.mkdir()
    session = session_module.SaveSession(device, work, log=lambda message: None)
    return session, pushed


def test_pull_marks_the_container_incomplete_until_a_push_succeeds(working_session):
    """A job that dies half way leaves a marker, so the next run can warn about it."""
    session, _ = working_session
    assert session.in_progress() is None
    session.pull(with_prefs=False)
    marker = session.in_progress()
    assert marker is not None and marker["container"] == "/c"

    session.push(session.live, with_prefs=False)
    assert session.in_progress() is None


def test_a_failed_push_keeps_the_marker(working_session, monkeypatch):
    session, _ = working_session
    session.pull(with_prefs=False)
    monkeypatch.setattr(session_module.dev, "push_tree",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("device gone")))
    with pytest.raises(RuntimeError):
        session.push(session.live, with_prefs=False)
    assert session.in_progress() is not None


def test_unlock_puts_the_backup_back_when_the_readback_mismatches(working_session, monkeypatch):
    """The phone must never keep a save the tool could not verify."""
    session, pushed = working_session
    monkeypatch.setattr(session_module, "SaveWorkspace", lambda root: object())
    monkeypatch.setattr(session_module, "describe_state", lambda workspace: {"ok": True})
    monkeypatch.setattr(session_module, "apply_unlocks", lambda w, o, log=None: {"changes": 1})
    monkeypatch.setattr(session_module.SaveSession, "readback_check",
                        lambda self, expected: {"files": 1, "matched": 0, "mismatched": ["game.data"],
                                                "missing": [], "extra": []})
    with pytest.raises(RuntimeError) as error:
        session.unlock()
    assert "pushed back" in str(error.value)
    assert pushed[-1].endswith("backups/before-unlock/Documents")   # the backup, not the patched copy


def test_unlock_puts_the_pulled_save_back_when_it_stops_before_patching(working_session, monkeypatch):
    """A failure before anything is changed must not leave the phone without its files."""
    session, pushed = working_session
    monkeypatch.setattr(session_module, "SaveWorkspace",
                        lambda root: (_ for _ in ()).throw(RuntimeError("no save files")))
    with pytest.raises(RuntimeError):
        session.unlock()
    assert pushed, "the pulled copy has to go back"
    assert pushed[-1].endswith("/live/Documents")
