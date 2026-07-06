"""neewer-client — a thin async client for a running ``neewerd`` daemon.

This is the **client boundary**: anything that wants to drive the lights *through
the daemon* (rather than owning Bluetooth itself) talks to the daemon's
``/api/v1`` HTTP layer via :class:`DaemonClient`. It imports no ``bleak`` and no
``neewer`` BLE code — a client only needs ``urllib`` and JSON — so a script,
``neewerctl``, or the ``neewer-mcp`` server can depend on it without pulling in
the whole BLE stack.

Two primitives cover everything the daemon exposes:

* :meth:`DaemonClient.run_command` — send one command-grammar line
  (``<target> <action> [args]``) through the ``{"cmd": …}`` escape hatch and get
  its reply string back (raising :class:`DaemonError` on a 4xx/5xx).
* :meth:`DaemonClient.get_json` — read a discovery endpoint (``/api/v1/state``,
  ``/api/v1/presets``).

The blocking ``urllib`` calls run in a worker thread so an async caller's event
loop is never stalled. Point the client at a daemon with ``[modules.http]``
enabled — bind ``127.0.0.1``; the API has no auth/TLS.

(Historically this lived inside :mod:`neewerd.mcp_server`; it was promoted here so
``neewerctl`` / ``neewer-mcp`` / ad-hoc scripts share one client, not three.)
"""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

#: Default daemon endpoint. Loopback because the http module has no auth/TLS.
DEFAULT_HTTP_URL = "http://127.0.0.1:8099"

#: The structured REST namespace on the daemon.
API = "/api/v1"

#: HTTP timeout for a single daemon round-trip (seconds). Localhost is fast; this
#: only needs to be generous enough that a busy daemon reply isn't cut off.
HTTP_TIMEOUT = 5.0


class DaemonError(Exception):
    """A daemon-side failure (bad args, unknown target/preset) or an unreachable
    daemon. Callers surface the message to the user / model as an error rather than
    a silent empty result."""


def _http_sync(method: str, url: str, body_obj=None, timeout: float = HTTP_TIMEOUT):
    """Blocking one-shot HTTP request. Returns ``(status, text)``.

    A transport-level failure (daemon down, connection refused) raises
    :class:`DaemonError` with a hint; an HTTP error *status* (400/404/500) is
    returned normally so the caller can read the ``{"error": …}`` body.
    """
    data = json.dumps(body_obj).encode() if body_obj is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:              # a real HTTP status (4xx/5xx)
        return exc.code, exc.read().decode(errors="replace")
    except (urllib.error.URLError, OSError) as exc:    # couldn't even connect
        reason = getattr(exc, "reason", exc)
        raise DaemonError(
            f"cannot reach neewerd at {url}: {reason}. Is the daemon running "
            f"with [modules.http] enabled?"
        ) from exc


class DaemonClient:
    """Thin async wrapper over the daemon's ``/api/v1`` layer.

    The blocking ``urllib`` calls run in a worker thread so the caller's event loop
    is never stalled. Two primitives cover everything: :meth:`run_command` (send a
    grammar line, get its reply) and :meth:`get_json` (read a discovery endpoint).
    """

    def __init__(self, base_url: str = DEFAULT_HTTP_URL):
        self.base = base_url.rstrip("/")

    async def run_command(self, line: str) -> str:
        """Run one command line via the ``{"cmd": …}`` escape hatch; return the
        ``result`` string, or raise :class:`DaemonError` carrying the daemon's
        ``error`` (so a 404 ``no tubes …`` / 400 ``bad args`` reaches the caller)."""
        status, text = await asyncio.to_thread(
            _http_sync, "POST", f"{self.base}{API}/command", {"cmd": line})
        payload = json.loads(text) if text.strip() else {}
        if status == 200:
            return str(payload.get("result", ""))
        raise DaemonError(str(payload.get("error") or f"HTTP {status}"))

    async def get_json(self, path: str):
        """GET a JSON endpoint (e.g. ``/api/v1/state`` or ``/api/v1/presets``)."""
        status, text = await asyncio.to_thread(
            _http_sync, "GET", f"{self.base}{path}")
        if status == 200:
            return json.loads(text) if text.strip() else None
        raise DaemonError(f"GET {path} -> HTTP {status}: {text[:200]}")
