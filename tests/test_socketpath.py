"""Tests for :mod:`neewerd.socketpath` — the daemon/CLI socket-path resolver.

The precedence ($NEEWERD_SOCKET > $XDG_RUNTIME_DIR > /run/neewerd > /tmp) is what
keeps neewerctl looking where neewerd listens, so each rung is exercised with the
environment controlled via monkeypatch.
"""
from __future__ import annotations

from neewerd import socketpath


def test_explicit_env_override_wins(monkeypatch):
    monkeypatch.setenv("NEEWERD_SOCKET", "/custom/neewerd.sock")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    assert socketpath.default_socket_path() == "/custom/neewerd.sock"


def test_xdg_runtime_dir_used_when_present(monkeypatch, tmp_path):
    monkeypatch.delenv("NEEWERD_SOCKET", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert socketpath.default_socket_path() == str(tmp_path / "neewerd.sock")


def test_xdg_ignored_when_dir_missing(monkeypatch):
    monkeypatch.delenv("NEEWERD_SOCKET", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/nonexistent/runtime/dir")
    # Falls through past the missing XDG dir; on a test box /run/neewerd won't
    # exist either, so we land on the /tmp fallback.
    assert socketpath.default_socket_path() == "/tmp/neewerd.sock"


def test_tmp_fallback_when_nothing_set(monkeypatch):
    monkeypatch.delenv("NEEWERD_SOCKET", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert socketpath.default_socket_path() == "/tmp/neewerd.sock"
