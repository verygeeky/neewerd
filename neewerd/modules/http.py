"""Minimal REST front-end built on the stdlib asyncio server (no extra deps).

Two surfaces, one engine. Both funnel into ``core.dispatch()`` — there is no
second command grammar here, only different ways to spell the same line.

* **Legacy / line transport** — POST a raw command line, GET the state snapshot::

      curl -X POST bertil:8099/cmd -d 'all hsi 240 100 80'
      curl bertil:8099/state

* **``/api/v1`` sugar** — resource-ish URLs + JSON bodies + real HTTP status
  codes (``200`` ok, ``400`` bad args / unknown verb, ``404`` no such target,
  ``422`` no addressed fixture supports the command)::

      curl -X POST bertil:8099/api/v1/all/hsi   -d '{"h":240,"s":100,"i":80}'
      curl -X POST bertil:8099/api/v1/t1/power  -d '{"on":false}'
      curl              bertil:8099/api/v1/state

The ``/api/v1`` layer is a thin shim: it shuffles the path segments and the JSON
fields into a ``<target> <action> [args...]`` line and hands it to the same
``core.dispatch()`` every other module uses. It holds no argument-order table of
its own — the field names and their order come from the typed command model
(:data:`neewer.protocol.commands.ACTIONS`), so there is one source of that truth.

This is still a deliberately tiny HTTP/1.1 implementation: it reads one request,
runs one command (or returns state), writes one response, and closes. It is not
a general web server — no keep-alive, no chunked transfer, no auth/TLS.

Two additions serve the bundled UIs without changing that model:

* **Static pages** — ``GET /`` serves the basic ``index.html``; ``GET /console``
  serves the richer v2 ``console.html``. Both are read from disk per request.
* **Discovery** — ``GET /api/v1/catalog`` serves the library's machine-readable
  protocol catalogue (:mod:`neewer.catalog`: actions, scene/pixel tables, flow
  options, gel brands) and ``GET /api/v1/targets`` the addressable target words
  (tubes + device-book groups/aliases), so no client hard-codes protocol facts.
* **``GET /api/v1/events``** (alias ``/events``) — the one route that keeps its
  socket open: a ``text/event-stream`` that pushes the state snapshot on connect
  and every :data:`SSE_INTERVAL` seconds thereafter, so a page reflects live
  connection/power/telemetry without polling. Control still flows over POST
  ``/api/v1`` — SSE is a one-way *down* channel, which is why plain POSTs (not a
  WebSocket) remain the *up* channel.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from neewer import catalog
from neewer.devices import normalize_netid
from neewer.errors import NeewerError, UnknownPreset, UnknownTarget, Unsupported
from neewer.grammar import parse as parse_command
from neewer.protocol import commands

log = logging.getLogger("neewerd.http")

#: URL namespace for the resource-ish sugar layer.
API_PREFIX = "/api/v1"

#: The single-page web UI bundled alongside this module. Served same-origin (so
#: it shares the API's origin and dodges CORS entirely).
UI_FILE = Path(__file__).resolve().parent / "ui" / "index.html"

#: The richer "console" UI (v2). A second self-contained page served at /console;
#: the basic UI above stays put at / so neither breaks the other.
CONSOLE_FILE = Path(__file__).resolve().parent / "ui" / "console.html"

#: GET paths that serve the basic UI page. Kept tiny on purpose — this is the one
#: and only static file the server hands out at the root.
_UI_PATHS = ("", "/ui", "/index.html")

#: GET paths that serve the v2 console page (added alongside, not replacing, /).
_CONSOLE_PATHS = ("/console", "/console.html")

#: GET paths that open a Server-Sent-Events stream of the state snapshot. The
#: canonical one lives under the API namespace; the bare alias is a convenience.
_SSE_PATHS = (API_PREFIX + "/events", "/events")

#: Heartbeat interval (seconds) for the SSE stream. The stream now pushes on
#: change (via the core's change-event API), so this is only a periodic keepalive
#: re-push when nothing has changed. One snapshot is always sent on connect.
SSE_INTERVAL = 2.0

#: HTTP reason phrases for the handful of statuses this server emits.
_REASON = {200: "OK", 400: "Bad Request", 404: "Not Found",
           413: "Payload Too Large", 422: "Unprocessable Entity",
           500: "Internal Server Error"}

#: Cap request bodies. The API only ever takes tiny JSON / command lines, so a
#: larger ``Content-Length`` is a mistake or an attempt to make us block/allocate
#: on the promise — reject it up front instead of reading it.
MAX_BODY = 64 * 1024

#: Loopback addresses that don't warrant the "no-auth, network-exposed" warning.
_LOOPBACK = ("127.0.0.1", "localhost", "::1")


class _HttpError(Exception):
    """A request-level error carrying the HTTP status to answer with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


async def run(core, cfg) -> None:
    """Serve the tiny REST endpoint forever."""
    # Default to loopback: the server has no auth/TLS and accepts raw frames, so a
    # non-loopback bind must be a deliberate choice, not a silent default.
    host = cfg.get("host", "127.0.0.1")
    port = int(cfg.get("port", 8099))

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handle one request: parse, route, respond, close."""
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            request_line = head.split(b"\r\n", 1)[0].decode(errors="replace")
            method, path, *_ = request_line.split(" ")
            body = await _read_body(reader, head)

            # SSE hijacks the socket for a long-lived stream, so it can't go
            # through the one-request/one-response/close path below — handle it
            # here and return (the connection is closed in ``finally``).
            if method == "GET" and _is_sse_path(path):
                await _serve_sse(core, writer)
                return

            status, content_type, payload = await _route(core, method, path, body)
            writer.write(_response(payload, status, content_type))
            await writer.drain()
        except _HttpError as exc:                    # e.g. an over-large body -> 413
            _safe_write(writer, _response(str(exc), exc.status))
        except Exception as exc:
            # Unlike the old version, we answer with a 400 instead of silently
            # dropping the connection — a broken client should see *why*.
            log.debug("http error: %s", exc)
            _safe_write(writer, _response(f"bad request: {exc}", 400))
        finally:
            writer.close()

    server = await asyncio.start_server(handle, host, port)
    if host not in _LOOPBACK:
        log.warning("http bound to %s (non-loopback) with NO auth/TLS — anyone on the "
                    "network can control the lights and send raw BLE frames. Bind "
                    "127.0.0.1 unless network exposure is intended.", host)
    log.info("http listening on %s:%s", host, port)
    async with server:
        await server.serve_forever()


# ---- state enrichment ----------------------------------------------------
#: The advert unit-id suffix: ``NW-<serial>&<networkId>`` — the ``&`` followed by
#: the 32-bit networkId hex. We pull it off the tube's advertised ``name`` to key
#: the device book's ``[units]`` display labels.
_UNIT_SUFFIX_RE = re.compile(r"&([0-9A-Fa-f]{1,8})\b")


def _unit_netid(name: str | None) -> str | None:
    """Parse the ``&XXXXXXXX`` networkId suffix out of an advertised tube name.

    Returns the id normalised to lowercase 8-hex-digit form (e.g. ``"00900002"``),
    or ``None`` if the name carries no ``&`` suffix. Never raises — a tube with a
    plain name just yields ``None``.
    """
    if not name:
        return None
    match = _UNIT_SUFFIX_RE.search(name)
    if not match:
        return None
    return normalize_netid(match.group(1))


def enriched_snapshot(core) -> dict:
    """``core.snapshot()`` with per-tube unit identity and governor telemetry folded in.

    On top of the library snapshot this helper adds, per tube:

    * ``unit`` — the raw ``networkId`` hex parsed from the tube's advertised
      ``name`` (the ``&XXXXXXXX`` suffix), or ``None`` if the name has none.
    * ``unit_name`` — the device book's configured display label for that
      networkId (``core.book.unit_name(...)``), or ``None`` if there's no book or
      no configured label.
    * ``gov`` — the artnet write-governor's ``stats()`` for tubes the artnet
      module has driven (the governors live in ``core.write_governors``, absent
      until that module runs). Tubes never driven simply gain no ``gov`` key.

    So the console gets everything we know about a tube — identity/telemetry, its
    stable unit-id, *and* its BLE write pacing — from the one state payload it
    already consumes (``/api/v1/state`` and the SSE stream).

    A shallow per-tube copy is made so we never mutate the core's cached dicts.
    Everything degrades gracefully: no book, no ``&`` suffix, or no governor each
    just yields ``None``/absent rather than an error.
    """
    snap = core.snapshot()
    governors = getattr(core, "write_governors", {}) or {}
    book = getattr(core, "book", None)
    enriched = {}
    for mac, tube in snap.items():
        merged = dict(tube)             # shallow copy — don't touch the core's cache
        netid = _unit_netid(tube.get("name"))
        merged["unit"] = netid
        merged["unit_name"] = book.unit_name(netid) if (book and netid) else None
        gov = governors.get(mac)
        if gov is not None:
            merged["gov"] = gov.stats()
        enriched[mac] = merged
    return enriched


def _targets(core) -> dict:
    """Every addressable target word, for a UI's target picker.

    Three sections, all read-only:

    * ``tubes`` — per-tube entries keyed by MAC: the preferred ``target`` word
      (``t<pos>`` when the tube has a physical position, else the MAC), plus the
      identity/capability fields a picker wants to show (``name``/``pos``/
      ``connected``/``model``/``caps``).
    * ``groups`` — the device book's ``[groups]`` verbatim (members may be
      aliases, MACs, or nested group names — the daemon resolves them; a UI can
      shallow-expand for capability gating).
    * ``aliases`` — the book's ``[aliases]`` nickname -> MAC map.

    Degrades gracefully: no device book just yields empty groups/aliases.
    """
    snap = core.snapshot()
    book = getattr(core, "book", None)
    tubes = {}
    for mac, tube in snap.items():
        pos = tube.get("pos")
        tubes[mac] = {
            "target": f"t{pos}" if pos is not None else mac,
            "name": tube.get("name"),
            "pos": pos,
            "connected": tube.get("connected"),
            "model": tube.get("model"),
            "caps": tube.get("caps"),
        }
    return {
        "tubes": tubes,
        "groups": dict(getattr(book, "groups", None) or {}),
        "aliases": dict(getattr(book, "aliases", None) or {}),
    }


# ---- routing -------------------------------------------------------------
async def _route(core, method: str, path: str, body: bytes):
    """Resolve a request to ``(status, content_type, payload)``.

    Routes, checked in order:

    1. ``GET /`` · ``/ui`` · ``/index.html`` — the bundled web UI page.
    2. ``/state`` (and ``/api/v1/state``) — the one non-command route; the cached
       snapshot, served as real JSON.
    3. ``GET /api/v1/presets`` — preset discovery: names + their command lines.
    4. ``GET /api/v1/catalog`` — the library's protocol catalogue (one JSON
       blob); ``GET /api/v1/targets`` — addressable targets + groups/aliases.
    5. ``/api/v1/...`` — the sugar layer (JSON in, JSON out, status codes). This
       includes ``POST /api/v1/preset/<name>`` (runs it; 404 on unknown name).
    6. anything else — the legacy line transport: command in the POST body, or
       the path-as-command fallback (``GET /all/power/off``).
    """
    path = path.split("?", 1)[0]                    # ignore any query string

    # The bundled UIs are the only static assets we serve; GET only. The basic
    # page keeps the root; the richer console lives at /console.
    if method == "GET" and path.rstrip("/") in _UI_PATHS:
        return _serve_static(UI_FILE, "web ui")
    if method == "GET" and path.rstrip("/") in _CONSOLE_PATHS:
        return _serve_static(CONSOLE_FILE, "console ui")

    if path.startswith("/state") or path.rstrip("/") == API_PREFIX + "/state":
        return 200, "application/json", json.dumps(enriched_snapshot(core))

    # Preset discovery: names + their command lines. Must be intercepted before
    # the generic /api/v1 router, which would try to parse "presets" as a command.
    # Presets are a daemon policy registered as the `preset` verb, so read the table
    # off that runner — the core library carries no preset attribute.
    if method == "GET" and path.rstrip("/") == API_PREFIX + "/presets":
        runner = core.verbs.get("preset")
        table = getattr(runner, "presets", {}) if runner is not None else {}
        return 200, "application/json", json.dumps(table)

    # Catalogue discovery: the library's machine-readable protocol catalogue
    # (actions/scenes/pixel/flows/gel brands) as one JSON blob. Pure data,
    # static per daemon version — intercepted before the generic command router.
    if method == "GET" and path.rstrip("/") == API_PREFIX + "/catalog":
        return 200, "application/json", json.dumps(catalog.catalog())

    # Target discovery: every addressable word — per-tube targets plus the
    # device book's groups and aliases — so a UI can populate its target picker.
    if method == "GET" and path.rstrip("/") == API_PREFIX + "/targets":
        return 200, "application/json", json.dumps(_targets(core))

    if path == API_PREFIX or path.startswith(API_PREFIX + "/"):
        return await _route_api(core, path, body)

    # Legacy line transport. Body wins; otherwise the path becomes the command
    # (e.g. GET /all/power/off -> 'all power off').
    cmd = body.decode().strip() or path.lstrip("/").replace("/", " ")
    return await _run(core, cmd, as_json=False)


def _serve_static(file: Path, label: str):
    """Read and return a bundled HTML page as an HTML response.

    Files are small and read per request (no caching) — fine for a LAN tool and
    it means editing a page needs no daemon restart. A missing file is a 404
    rather than an exception, so the API keeps working without the UI present.
    """
    try:
        html = file.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("%s not available: %s", label, exc)
        return 404, "text/plain", f"{label} not found: {exc}"
    return 200, "text/html; charset=utf-8", html


# ---- Server-Sent Events (live state push) --------------------------------
def _is_sse_path(path: str) -> bool:
    """True if ``path`` (query/trailing-slash tolerant) requests the SSE stream."""
    return path.split("?", 1)[0].rstrip("/") in _SSE_PATHS


def _sse_headers() -> bytes:
    """HTTP head for a Server-Sent-Events stream.

    Deliberately unlike :func:`_response`: an SSE body is an *unending* sequence
    of ``data:`` records, so there is no ``Content-Length`` and the socket stays
    open. ``X-Accel-Buffering: no`` asks reverse proxies not to buffer the stream.
    """
    return (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/event-stream\r\n"
        "Cache-Control: no-cache\r\n"
        "Connection: keep-alive\r\n"
        "X-Accel-Buffering: no\r\n"
        "\r\n"
    ).encode()


def _sse_event(payload: str, event: str | None = None) -> bytes:
    """Format one SSE message: an optional ``event:`` line, ``data:`` line(s), blank.

    A record is terminated by a blank line. ``payload`` is emitted as one or more
    ``data:`` lines (SSE requires every physical line of a multi-line datum to be
    prefixed), so a browser's ``EventSource`` reassembles it into one message.
    """
    lines = []
    if event is not None:
        lines.append(f"event: {event}")
    for line in payload.split("\n") or [""]:
        lines.append(f"data: {line}")
    return ("\n".join(lines) + "\n\n").encode()


async def _serve_sse(core, writer, *, interval: float = SSE_INTERVAL,
                     max_events: int | None = None) -> None:
    """Stream the state snapshot to one client as Server-Sent-Events.

    Emits the current snapshot immediately (so a fresh page paints at once), then
    re-emits **on change** — the core's change-event API (:meth:`Fleet.subscribe`)
    wakes this loop whenever a tube connects/drops, reports telemetry, or takes a
    command, so a browser reflects live state with no polling on either side. The
    ``interval`` is now just a keepalive heartbeat (a periodic re-push even when
    nothing changed). Falls back to pure heartbeat if the core predates the API.
    Runs until the client disconnects (a write raises) or the daemon shuts down.

    ``max_events`` bounds the loop — ``None`` streams forever in production; the
    tests pass a small integer so the coroutine returns after emitting.
    """
    writer.write(_sse_headers())
    await writer.drain()

    changed = asyncio.Event()
    changed.set()                       # push an initial snapshot on connect
    unsubscribe = core.subscribe(changed.set) if hasattr(core, "subscribe") else None
    try:
        sent = 0
        while max_events is None or sent < max_events:
            writer.write(_sse_event(json.dumps(enriched_snapshot(core)), event="state"))
            await writer.drain()
            sent += 1
            if max_events is not None and sent >= max_events:
                break
            # Wake on the next change, or after the heartbeat interval — whichever
            # comes first — then loop and re-push.
            try:
                await asyncio.wait_for(changed.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            changed.clear()
    finally:
        if unsubscribe is not None:
            unsubscribe()


async def _route_api(core, path: str, body: bytes):
    """Map a ``/api/v1`` request onto a command line and run it.

    Body shapes accepted (all optional):

    * ``{"cmd": "all hsi 240 100 80"}`` — full escape hatch; path is ignored.
    * ``{"h":240,"s":100,"i":80}`` — named fields for the action in the path.
    * ``{"on": false}`` — boolean for ``power``.
    * ``{"args": [240,100,80]}`` or a bare JSON array — positional override.
    """
    try:
        data = _decode_json(body)
        if isinstance(data, dict) and "cmd" in data:
            return await _run(core, str(data["cmd"]), as_json=True)

        segments = [s for s in path[len(API_PREFIX):].strip("/").split("/") if s]
        if not segments:
            raise ValueError("empty path; try /api/v1/<target>/<action>")

        base = " ".join(segments)
        # parse() both validates the <target> <action> shape and tells us which
        # word is the action, so we can map named JSON fields onto positional args.
        action = parse_command(base).action
        args = _args_from_body(action, data)
        line = (base + " " + " ".join(args)).strip()
    except ValueError as exc:
        return _result(str(exc), 400, as_json=True, ok=False)

    return await _run(core, line, as_json=True)


#: Which HTTP status each library error type maps to. The library raises
#: transport-agnostic :mod:`neewer.errors`; owning the status here keeps HTTP
#: knowledge in HTTP (no more sniffing the reply string for a prefix). Anything
#: else in the ``NeewerError`` family — ``UnknownAction`` / ``UnknownEffect`` —
#: is a malformed request → 400.
_ERROR_STATUS = {
    UnknownTarget: 404,     # target resolved to no connected tubes
    UnknownPreset: 404,     # no such preset
    Unsupported: 422,       # well-formed, but no addressed fixture can do it
}


def _status_for_error(exc: NeewerError) -> int:
    """Map a library error to an HTTP status (default 400 for the bad-request family)."""
    for cls, status in _ERROR_STATUS.items():
        if isinstance(exc, cls):
            return status
    return 400


async def _run(core, cmd: str, as_json: bool):
    """Dispatch one command line and classify the outcome into an HTTP status.

    ``dispatch`` raises a typed :mod:`neewer.errors` error for every handled
    failure (mapped to a status by :func:`_status_for_error`), ``ValueError`` for
    malformed arguments (→ 400), or returns a human string on success (→ 200).
    Status codes are an HTTP concern, so the mapping lives here, not in ``core``.
    """
    if not cmd:
        return _result("empty command", 400, as_json, ok=False)
    try:
        reply = str(await core.dispatch(cmd))
    except NeewerError as exc:                       # typed command error
        return _result(str(exc), _status_for_error(exc), as_json, ok=False)
    except ValueError as exc:                        # bad / garbled arguments
        return _result(str(exc), 400, as_json, ok=False)
    except Exception as exc:                          # unexpected — don't leak a stack
        log.debug("dispatch error: %s", exc)
        return _result(f"error: {exc}", 500, as_json, ok=False)

    return _result(reply, 200, as_json, ok=True)


# ---- JSON body -> command args ------------------------------------------
def _args_from_body(action: str, data) -> list[str]:
    """Turn a decoded JSON body into the trailing command args for ``action``.

    The scalar-field names, their order, and any trailing variadic list come from
    the typed command model (:data:`neewer.protocol.commands.ACTIONS`) — the REST
    layer no longer keeps its own copy, so a command's argument order lives in
    exactly one place and can't silently drift out of sync here.
    """
    if data is None:
        return []
    if isinstance(data, list):                      # bare positional array
        return [_token(x) for x in data]
    if not isinstance(data, dict):                  # bare scalar
        return [_token(data)]
    if "args" in data:                              # explicit positional override
        return [_token(x) for x in data["args"]]
    if action == "power":                           # boolean, not a scalar-field list
        on = data.get("on")
        truthy = on in (True, 1, "1", "on", "true", "True")
        return ["on" if truthy else "off"]
    spec = commands.ACTIONS.get(action)
    if spec is None:
        return []
    args = [_token(data[field]) for field in spec.fields if field in data]
    if spec.variadic:                               # scene params / pixel colours
        args += [_token(x) for x in data.get(spec.variadic, [])]
    return args


def _token(value) -> str:
    """Render a JSON scalar as a single command-line word.

    JSON numbers decode to ``int``/``float``; an integral float (``240.0`` from
    ``240``) must render as ``240``, not ``240.0``, so the int parser downstream
    is happy.
    """
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _decode_json(body: bytes):
    """Decode a JSON request body, or ``None`` if it is empty."""
    if not body or not body.strip():
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON body: {exc}") from exc


# ---- http plumbing -------------------------------------------------------
def _result(text: str, status: int, as_json: bool, ok: bool):
    """Build a ``(status, content_type, payload)`` triple.

    On the ``/api/v1`` layer (``as_json``) replies are wrapped as
    ``{"result": ...}`` / ``{"error": ...}``; the legacy layer returns the bare
    string for back-compat with existing ``curl``/scripts.
    """
    if as_json:
        key = "result" if ok else "error"
        return status, "application/json", json.dumps({key: text})
    return status, "text/plain", text


def _safe_write(writer, data: bytes) -> None:
    """Best-effort write of a final response; ignore an already-broken client."""
    try:
        writer.write(data)
    except Exception:
        pass


async def _read_body(reader: asyncio.StreamReader, head: bytes) -> bytes:
    """Read the request body if a ``Content-Length`` header is present.

    Rejects an over-large ``Content-Length`` (:data:`MAX_BODY`) with 413 rather
    than blocking/allocating on the promised bytes.
    """
    for header in head.decode(errors="replace").split("\r\n"):
        if header.lower().startswith("content-length:"):
            try:
                length = int(header.split(":", 1)[1])
            except ValueError:
                return b""
            if length > MAX_BODY:
                raise _HttpError(413, f"request body too large "
                                      f"({length} > {MAX_BODY} bytes)")
            return await reader.readexactly(length)
    return b""


def _response(payload: str, status: int = 200, content_type: str = "text/plain") -> bytes:
    """Build a complete, connection-closing HTTP/1.1 response.

    The body is encoded first so ``Content-Length`` is byte-accurate even when
    the payload (JSON state, error text) contains non-ASCII characters.
    """
    reason = _REASON.get(status, "OK")
    body = payload.encode()
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    return head + body
