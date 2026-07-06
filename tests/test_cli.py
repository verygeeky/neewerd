"""Tests for :mod:`neewerd.cli` — the thin socket client.

``_format`` is pure and tested directly. ``send_line`` talks a real Unix socket,
so we stand up a tiny in-process echo server on a temp socket in a background
thread — no daemon, no BLE, just the wire contract (send one line, read one line).
"""
from __future__ import annotations

import socket
import threading

import pytest

from neewerd import cli


def _serve_once(path: str, reply: bytes, captured: list) -> threading.Thread:
    """Start a one-shot AF_UNIX server that records the request and sends ``reply``."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)

    def run():
        conn, _ = server.accept()
        with conn:
            captured.append(conn.recv(4096))
            conn.sendall(reply)
        server.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_send_line_round_trip(tmp_path):
    path = str(tmp_path / "test.sock")
    captured: list = []
    thread = _serve_once(path, b"ok hsi -> 1 tube(s)\n", captured)

    reply = cli.send_line(path, "all hsi 240 100 80")

    thread.join(timeout=2)
    assert reply == "ok hsi -> 1 tube(s)"
    # The client appends exactly one newline and no more.
    assert captured[0] == b"all hsi 240 100 80\n"


def test_send_line_refused_when_no_server(tmp_path):
    path = str(tmp_path / "absent.sock")
    with pytest.raises((ConnectionRefusedError, FileNotFoundError)):
        cli.send_line(path, "all power on")


def test_format_pretty_prints_json():
    out = cli._format('{"b": 2, "a": 1}', as_json=True)
    # Pretty-printed and key-sorted.
    assert out == '{\n  "a": 1,\n  "b": 2\n}'


def test_format_passes_through_non_json_when_json_requested():
    # An "ok ..." line isn't JSON; it should survive --json untouched.
    assert cli._format("ok power -> 1 tube(s)", as_json=True) == "ok power -> 1 tube(s)"


def test_format_returns_reply_verbatim_without_json_flag():
    assert cli._format('{"a":1}', as_json=False) == '{"a":1}'
