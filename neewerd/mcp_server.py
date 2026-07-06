"""neewer-mcp — an MCP (Model Context Protocol) server for the Neewer tubes.

A standalone stdio process that lets an AI assistant (Claude, etc.) drive the
lights conversationally: "set the key lights warm at 40%", "start a palette
flow", "what's the battery on tube 2?".

It is a **client of a running neewerd daemon**, not a second Bluetooth central —
it talks to the daemon's ``/api/v1`` HTTP layer and never imports ``bleak`` or
``NeewerCore``. That keeps the single-owner-of-Bluetooth invariant: ``core`` is
still the only thing that holds BLE links. Every tool composes a command-grammar
line (``<target> <action> [args]``) and sends it through the ``/api/v1`` ``cmd``
escape hatch, so there is exactly one command grammar — the same one every other
transport funnels into ``core.dispatch``.

Why HTTP (not the Unix socket): the ``/api/v1`` layer already classifies replies
into ``200``/``400``/``404``/``500`` and wraps them as ``{"result"}/{"error"}``,
which maps directly onto "MCP tool result vs tool error". Point it at a daemon
with ``[modules.http]`` enabled (bind ``127.0.0.1`` — the API has no auth/TLS).

Run it from an assistant's MCP config as ``neewer-mcp`` (installed by
``pip install 'neewerd[mcp]'``). Configure the daemon endpoint with
``--http-url`` or ``$NEEWER_MCP_URL`` (default ``http://127.0.0.1:8099``).

Layout note: the translation + transport helpers below import nothing from the
``mcp`` SDK, so the unit tests exercise them with the SDK absent (CI installs
only pytest). Only :func:`build_app` / :func:`main` need ``mcp``.
"""
from __future__ import annotations

import argparse
import json
import os

# The daemon client is its own module now (the client boundary): neewerctl,
# neewer-mcp and ad-hoc scripts all share it. Re-exported here so existing
# ``from neewerd.mcp_server import DaemonClient, DaemonError`` keeps working.
from .client import API, DEFAULT_HTTP_URL, DaemonClient, DaemonError

#: The live client, set by :func:`main` (and by tests). Kept module-global so the
#: tool functions stay plain ``async def``\ s that FastMCP can introspect.
_CLIENT: DaemonClient | None = None


def _client() -> DaemonClient:
    """Return the configured client, or raise if the server wasn't initialised."""
    if _CLIENT is None:
        raise DaemonError("MCP server not initialised (no daemon client)")
    return _CLIENT


# ---- tools (plain async functions; registered with FastMCP in build_app) -----
# Targets accepted everywhere: ``all`` / ``t<N>`` / a group or alias from the
# device book / a MAC — whatever ``core.resolve()`` understands. The tools pass
# the target through verbatim; they do no target parsing of their own.

async def list_lights() -> list[dict]:
    """List known tubes with their addressable target, name, position and whether
    they are currently connected."""
    snap = await _client().get_json(f"{API}/state") or {}
    lights = []
    for mac, tube in snap.items():
        pos = tube.get("pos")
        lights.append({
            "target": f"t{pos}" if pos else mac,
            "mac": mac,
            "name": tube.get("name", ""),
            "position": pos,
            "connected": bool(tube.get("connected")),
        })
    return lights


async def get_state(target: str = "all") -> dict:
    """Read the cached per-tube state snapshot (power, colour if known, battery),
    optionally filtered to a target."""
    reply = await _client().run_command(f"state {target}")
    return json.loads(reply) if reply.strip() else {}


async def power(target: str, on: bool) -> str:
    """Turn a target on or off."""
    return await _client().run_command(f"{target} power {'on' if on else 'off'}")


async def set_hsi(target: str, h: int, s: int, i: int) -> str:
    """Set a target's colour by hue (0-359), saturation (0-100) and intensity /
    brightness (0-100)."""
    return await _client().run_command(f"{target} hsi {h} {s} {i}")


async def set_rgbcw(target: str, bri: int, r: int = 0, g: int = 0, b: int = 0,
                    c: int = 0, w: int = 0) -> str:
    """Set a TL120C's colour with dedicated Cold/Warm white channels (by-MAC only):
    brightness (0-100), red/green/blue (0-255 each), and cold-white + warm-white
    (0-255 each) for a true high-CRI white plain HSI can't reach."""
    return await _client().run_command(f"{target} rgbcw {bri} {r} {g} {b} {c} {w}")


async def set_xy(target: str, bri: int, x: float, y: float) -> str:
    """Set a TL120C's colour by CIE-1931 chromaticity (by-MAC only): brightness
    (0-100) and the x/y coordinates (floats in 0.0-1.0, e.g. D65 white is
    x=0.3127, y=0.3290)."""
    return await _client().run_command(f"{target} xy {bri} {x} {y}")


async def set_gel(target: str, hue: int, sat: int, bri: int, brand: str = "rosco",
                  gel_no: int = 0) -> str:
    """Set a TL120C's gel / colour-paper colour (by-MAC only). Gel is an HSI colour
    plus brand/number metadata: hue (0-359), saturation (0-100), brightness (0-100),
    brand ("rosco"/"lee" or 1/2), and the gel's catalog number."""
    return await _client().run_command(f"{target} gel {hue} {sat} {bri} {brand} {gel_no}")


async def set_cct(target: str, bri: int, temp: int, gm: int = 50) -> str:
    """Set a target's white light: brightness (0-100), colour temperature in
    hundreds of kelvin (32-85 = 3200K-8500K), and green/magenta tint (0-100, 50
    neutral)."""
    return await _client().run_command(f"{target} cct {bri} {temp} {gm}")


async def set_brightness(target: str, bri: int) -> str:
    """Set a target's brightness only (0-100), leaving it a neutral white."""
    return await _client().run_command(f"{target} bri {bri}")


async def scene(target: str, effect: int, params: list[int] | None = None) -> str:
    """Run a built-in scene effect by id on a target, with optional numeric
    parameters."""
    tokens = " ".join(str(p) for p in (params or []))
    return await _client().run_command(f"{target} scene {effect} {tokens}".strip())


async def set_pixel(target: str, colors: list[str]) -> str:
    """Paint a per-segment pixel palette on a target (TL120C). Each colour is one
    segment band: a hue 0-359, "off" (dark), or "k<kelvin>" like "k3200"."""
    tokens = " ".join(str(c) for c in colors)
    return await _client().run_command(f"{target} pixel {tokens}")


async def start_flow(mode: str, opts: dict[str, str] | None = None) -> str:
    """Start a running animation across the whole fleet (modes: hue, comet,
    palette, tri, multistop), with optional ``key=value`` options like
    ``speed=0.05``."""
    tokens = " ".join(f"{k}={v}" for k, v in (opts or {}).items())
    return await _client().run_command(f"flow {mode} {tokens}".strip())


async def stop() -> str:
    """Stop any running flow / animation."""
    return await _client().run_command("stop")


async def query_status(target: str = "all") -> str:
    """Ask target tubes to report battery / state / version. Replies arrive
    asynchronously; read the ``neewer://state`` resource (or call ``get_state``)
    a moment later to see the refreshed values."""
    return await _client().run_command(f"query {target}")


async def list_presets() -> dict:
    """List configured presets (name -> the command lines each one runs)."""
    return await _client().get_json(f"{API}/presets") or {}


async def run_preset(name: str) -> str:
    """Run a named preset from the daemon's configuration."""
    return await _client().run_command(f"preset {name}")


#: Every tool, in the order they're registered. Kept as data so both build_app and
#: the tests iterate one list.
TOOLS = [
    list_lights, get_state, power, set_hsi, set_cct, set_brightness,
    set_rgbcw, set_xy, set_gel,
    scene, set_pixel, start_flow, stop, query_status, list_presets, run_preset,
]


# ---- resources -----------------------------------------------------------
async def state_resource() -> str:
    """The full live snapshot as JSON — lets the model read current state without
    spending a tool call."""
    snap = await _client().get_json(f"{API}/state")
    return json.dumps(snap, indent=2)


async def presets_resource() -> str:
    """The configured presets as JSON, for discovery."""
    return json.dumps(await _client().get_json(f"{API}/presets"), indent=2)


#: resource URI -> reader coroutine.
RESOURCES = {
    "neewer://state": state_resource,
    "neewer://presets": presets_resource,
}


# ---- config --------------------------------------------------------------
def resolve_http_url(args: argparse.Namespace) -> str:
    """Resolve the daemon URL: ``--http-url`` > ``$NEEWER_MCP_URL`` > default."""
    return args.http_url or os.environ.get("NEEWER_MCP_URL") or DEFAULT_HTTP_URL


def build_parser() -> argparse.ArgumentParser:
    """CLI parser for the ``neewer-mcp`` entry point."""
    parser = argparse.ArgumentParser(
        prog="neewer-mcp",
        description="MCP server exposing a running neewerd daemon to AI assistants.",
    )
    parser.add_argument(
        "--http-url",
        help=f"daemon /api/v1 base URL (default: $NEEWER_MCP_URL or {DEFAULT_HTTP_URL})",
    )
    return parser


# ---- MCP wiring (needs the optional 'mcp' extra) -------------------------
def build_app(client: DaemonClient):
    """Create the FastMCP app and register every tool + resource against it.

    Imported lazily so the module (and its helpers/tests) load without the ``mcp``
    package installed. FastMCP derives each tool's schema from its type hints and
    its description from the docstring, so the docstrings above are the model-facing
    API — keep them accurate.
    """
    from mcp.server.fastmcp import FastMCP

    global _CLIENT
    _CLIENT = client
    app = FastMCP("neewerd")
    for tool in TOOLS:
        app.tool()(tool)
    for uri, reader in RESOURCES.items():
        app.resource(uri)(reader)
    return app


def main(argv: list[str] | None = None) -> None:
    """Entry point: resolve config, build the app, serve over stdio."""
    args = build_parser().parse_args(argv)
    client = DaemonClient(resolve_http_url(args))
    try:
        app = build_app(client)
    except ImportError as exc:  # pragma: no cover - depends on the optional extra
        raise SystemExit(
            "neewer-mcp requires the 'mcp' package: pip install 'neewerd[mcp]'"
        ) from exc
    app.run()  # stdio transport (default)


if __name__ == "__main__":  # pragma: no cover
    main()
