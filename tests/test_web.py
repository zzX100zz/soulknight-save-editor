"""The web UI must start on a bare system Python and refuse to be driven by others."""

from __future__ import annotations

import http.client
import json
import threading
import urllib.parse
from http.server import ThreadingHTTPServer

import pytest

from sksave import web


@pytest.fixture()
def server(monkeypatch):
    monkeypatch.setattr(web, "devices", lambda: [])
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def request(url: str, *, method: str = "GET", payload: dict | None = None,
            headers: dict | None = None) -> tuple[int, bytes]:
    """http.client rather than urllib: this sandbox intercepts urllib's requests."""
    parts = urllib.parse.urlsplit(url)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    body = json.dumps(payload).encode() if payload is not None else None
    connection.request(method, parts.path or "/", body=body, headers=headers or {})
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response.status, data


def get(url: str) -> tuple[int, bytes]:
    return request(url)


def post(url: str, payload: dict, *, header: bool = True) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if header:
        headers["X-Requested-With"] = "sksave"
    status, body = request(url, method="POST", payload=payload, headers=headers)
    return status, json.loads(body or b"{}")


def test_page_is_served(server):
    status, body = get(server + "/")
    assert status == 200
    text = body.decode()
    assert "Soul Knight" in text
    assert "/api/job" in text          # the page drives the API


def test_status_reports_the_environment(server):
    status, body = get(server + "/api/status")
    assert status == 200
    payload = json.loads(body)
    assert set(payload["environment"]) >= {"venv", "dependencies", "airlift", "xcode"}
    assert payload["options"][0] == "heroes"
    assert payload["job"]["status"] in {"idle", "running", "done", "failed"}


def test_unknown_job_is_rejected(server):
    status, payload = post(server + "/api/job", {"name": "rm-rf"})
    assert status == 400
    assert "unknown job" in payload["error"]


def test_post_without_the_header_is_rejected(server):
    status, _ = post(server + "/api/job", {"name": "info"}, header=False)
    assert status == 403


def test_cross_origin_host_is_rejected(server):
    status, _ = request(server + "/api/status", headers={"Host": "evil.example"})
    assert status == 403


def test_cli_report_is_read_back(tmp_path, monkeypatch):
    """The CLI report is what carries the result into the page."""
    import argparse

    from sksave import cli

    report = tmp_path / "report.json"
    cli._write_report(argparse.Namespace(report=str(report)), {"state": {"uid": "1"}})
    assert json.loads(report.read_text())["state"]["uid"] == "1"
    cli._write_report(argparse.Namespace(report=None), {"state": {}})   # must not raise
