"""Tests for the v2 console page + the Server-Sent-Events endpoint in
:mod:`neewerd.modules.http`.

Like ``test_http.py`` these are hardware-free and socket-free: the console page
goes through the same ``_route`` helper, and the SSE stream is driven against an
in-memory fake writer (bounded with ``max_events`` so the streaming loop returns)
rather than a real TCP socket. The :class:`FakeCore` from ``test_http.py`` is
reused so both suites share one fake.
"""
from __future__ import annotations

import asyncio
import json

from test_http import FakeCore, _strip_unit_fields, route

from neewerd.modules import http


class FakeWriter:
    """Minimal ``asyncio.StreamWriter`` stand-in: collects everything written.

    Only the surface :func:`http._serve_sse` touches is implemented — ``write``
    buffers bytes and ``drain`` is a no-op coroutine. This lets us assert on the
    exact SSE bytes without opening a socket.
    """

    def __init__(self):
        self.chunks: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.chunks.append(bytes(data))

    async def drain(self) -> None:
        return None

    @property
    def data(self) -> bytes:
        return b"".join(self.chunks)


def serve_sse(core, **kwargs) -> bytes:
    """Run the SSE handler to completion and return the raw bytes it wrote."""
    writer = FakeWriter()
    asyncio.run(http._serve_sse(core, writer, interval=0, **kwargs))
    return writer.data


# --- console page ---------------------------------------------------------

def test_console_served_at_console_path():
    core = FakeCore()
    status, ctype, payload = route(core, "GET", "/console")
    assert status == 200 and ctype.startswith("text/html")
    assert "<html" in payload.lower() or "<!doctype" in payload.lower()
    assert core.dispatched == []                 # the page never hits dispatch


def test_console_trailing_slash_and_html_alias():
    core = FakeCore()
    for path in ("/console/", "/console.html"):
        status, ctype, _ = route(core, "GET", path)
        assert status == 200 and ctype.startswith("text/html"), path


def test_root_still_serves_basic_ui_unchanged():
    # v2 must not steal the root; the basic UI stays at /.
    core = FakeCore()
    status, ctype, _ = route(core, "GET", "/")
    assert status == 200 and ctype.startswith("text/html")


# --- SSE endpoint ---------------------------------------------------------

def test_is_sse_path_matches_both_aliases():
    assert http._is_sse_path("/api/v1/events")
    assert http._is_sse_path("/events")
    assert http._is_sse_path("/events/?x=1")     # trailing slash + query tolerated
    assert not http._is_sse_path("/api/v1/state")


def test_sse_headers_declare_event_stream():
    head = http._sse_headers().decode()
    assert head.startswith("HTTP/1.1 200 OK")
    assert "Content-Type: text/event-stream" in head
    assert "Cache-Control: no-cache" in head
    # An SSE body is unbounded, so it must NOT advertise a Content-Length.
    assert "Content-Length" not in head


def test_sse_event_formats_data_frame():
    frame = http._sse_event('{"a":1}', event="state").decode()
    assert frame == "event: state\ndata: {\"a\":1}\n\n"


def test_sse_event_prefixes_every_line_of_multiline_payload():
    frame = http._sse_event("one\ntwo").decode()
    assert frame == "data: one\ndata: two\n\n"


def test_serve_sse_emits_headers_then_state_frame():
    core = FakeCore(snap={"AA": {"name": "NW-AA", "pos": 1, "connected": True,
                                 "battery": 88, "version": "1.2.3"}})
    raw = serve_sse(core, max_events=1).decode()
    # Headers first...
    assert "Content-Type: text/event-stream" in raw
    # ...then at least one `data:` frame carrying the JSON snapshot.
    assert "event: state" in raw
    data_line = next(line for line in raw.splitlines() if line.startswith("data:"))
    payload = json.loads(data_line[len("data:"):].strip())
    assert _strip_unit_fields(payload) == core.snap
    assert core.dispatched == []                 # SSE never dispatches commands


def test_serve_sse_bounded_by_max_events():
    core = FakeCore()
    raw = serve_sse(core, max_events=3).decode()
    # One header block + exactly three state frames.
    assert raw.count("event: state") == 3


def test_serve_sse_subscribes_for_change_push_and_unsubscribes_on_exit():
    # When the core exposes the change-event API, SSE pushes on change (not poll):
    # it subscribes on connect and unsubscribes when the stream ends.
    class SubCore(FakeCore):
        def __init__(self):
            super().__init__()
            self.subscribed = 0
            self.unsubscribed = 0

        def subscribe(self, callback):
            self.subscribed += 1
            return lambda: setattr(self, "unsubscribed", self.unsubscribed + 1)

    core = SubCore()
    serve_sse(core, max_events=1)
    assert core.subscribed == 1
    assert core.unsubscribed == 1
