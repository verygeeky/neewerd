"""Tests for :mod:`neewerd.client` — the thin daemon HTTP client.

Hardware- and socket-free: the blocking HTTP helper (``_http_sync``) is
monkeypatched, so we assert the status mapping (200 -> value, 4xx/5xx ->
:class:`DaemonError`) and the ``{"cmd": …}`` escape-hatch wiring without a daemon.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from neewerd import client
from neewerd.client import DaemonClient, DaemonError


def run(coro):
    return asyncio.run(coro)


def _fake_http(status, text):
    def _inner(method, url, body_obj=None, timeout=client.HTTP_TIMEOUT):
        _inner.calls.append((method, url, body_obj))
        return status, text
    _inner.calls = []
    return _inner


def test_run_command_returns_result(monkeypatch):
    fake = _fake_http(200, json.dumps({"result": "ok hsi -> 4 tube(s)"}))
    monkeypatch.setattr(client, "_http_sync", fake)
    dc = DaemonClient("http://x:8099")
    assert run(dc.run_command("all hsi 240 100 80")) == "ok hsi -> 4 tube(s)"
    # sends the line via the {"cmd": ...} escape hatch
    assert fake.calls == [("POST", "http://x:8099/api/v1/command",
                           {"cmd": "all hsi 240 100 80"})]


def test_run_command_error_status_raises(monkeypatch):
    monkeypatch.setattr(client, "_http_sync",
                        _fake_http(404, json.dumps({"error": "no tubes for target 't9'"})))
    dc = DaemonClient("http://x:8099")
    with pytest.raises(DaemonError) as exc:
        run(dc.run_command("t9 power on"))
    assert "no tubes" in str(exc.value)


def test_get_json_ok_and_error(monkeypatch):
    monkeypatch.setattr(client, "_http_sync", _fake_http(200, json.dumps({"a": 1})))
    dc = DaemonClient("http://x:8099")
    assert run(dc.get_json("/api/v1/presets")) == {"a": 1}

    monkeypatch.setattr(client, "_http_sync", _fake_http(500, "boom"))
    with pytest.raises(DaemonError):
        run(dc.get_json("/api/v1/state"))


def test_http_sync_unreachable_raises_daemon_error(monkeypatch):
    import urllib.error

    def boom(*a, **k):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(client.urllib.request, "urlopen", boom)
    with pytest.raises(DaemonError) as exc:
        client._http_sync("GET", "http://127.0.0.1:9/api/v1/state")
    assert "cannot reach neewerd" in str(exc.value)
