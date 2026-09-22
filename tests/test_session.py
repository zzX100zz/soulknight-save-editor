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
