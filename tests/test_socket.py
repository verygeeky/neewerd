"""Dry tests for :mod:`neewerd.modules.socket` — the command-pipe handler.

Exercises ``_serve_client`` with in-memory asyncio streams (no real socket, no
radio): a fake core records dispatched lines and returns canned replies.
"""
from __future__ import annotations

import asyncio

from neewerd.modules import socket as sockmod


def run(coro):
    return asyncio.run(coro)


class FakeCore:
    def __init__(self, reply="ok cct -> 1 tube(s)"):
        self.reply = reply
        self.dispatched: list[str] = []

    async def dispatch(self, line):
        self.dispatched.append(line)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


async def _drive(core, request: bytes):
    """Feed ``request`` bytes through _serve_client and capture the reply bytes."""
    reader = asyncio.StreamReader()
    reader.feed_data(request)
    reader.feed_eof()

    class FakeWriter:
        def __init__(self):
            self.buf = bytearray()

        def write(self, data):
            self.buf += data

        async def drain(self):
            pass

        def close(self):
            pass

    writer = FakeWriter()
    await sockmod._serve_client(core, reader, writer)
    return bytes(writer.buf)


def test_dispatches_each_line_and_replies():
    core = FakeCore()
    out = run(_drive(core, b"all cct 50 32\n"))
    assert core.dispatched == ["all cct 50 32"]
    assert out == b"ok cct -> 1 tube(s)\n"


def test_multiple_lines_in_one_connection():
    core = FakeCore(reply="ok")
    out = run(_drive(core, b"all power on\nall power off\n"))
    assert core.dispatched == ["all power on", "all power off"]
    assert out == b"ok\nok\n"


def test_dispatch_error_is_reported_not_dropped():
    core = FakeCore(reply=ValueError("bad args"))
    out = run(_drive(core, b"all hsi x\n"))
    assert out == b"error: bad args\n"


def test_blank_line_still_dispatches_empty():
    # a bare newline reaches dispatch as "" (core decides what to do with it)
    core = FakeCore(reply="empty command")
    out = run(_drive(core, b"\n"))
    assert core.dispatched == [""]
    assert out == b"empty command\n"
