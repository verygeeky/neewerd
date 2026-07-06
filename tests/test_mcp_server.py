"""Tests for :mod:`neewerd.mcp_server` — the MCP server (a neewerd HTTP client).

The module's tool/translation/transport helpers import nothing from the ``mcp``
SDK, so these run in CI with only pytest installed (the SDK is an optional extra).
Two layers are exercised:

* **Tool -> command-line translation** — inject a fake daemon client and assert
  each tool composes the right grammar line / hits the right discovery endpoint.
* **Transport status mapping** — monkeypatch the one blocking HTTP helper and
  assert :class:`DaemonClient` turns ``{"result"}`` into a value and any error
  status / unreachable daemon into a :class:`DaemonError`.

Only ``build_app``/``main`` need the SDK; a single guarded test covers the wiring.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from neewerd import mcp_server
from neewerd.mcp_server import DaemonClient, DaemonError


def run(coro):
    return asyncio.run(coro)


class FakeClient:
    """Stands in for DaemonClient: records command lines and GET paths."""

    def __init__(self, result="ok", jsonval=None):
        self.commands: list[str] = []
        self.gets: list[str] = []
        self.result = result
        self.jsonval = jsonval if jsonval is not None else {}

    async def run_command(self, line: str):
        self.commands.append(line)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def get_json(self, path: str):
        self.gets.append(path)
        return self.jsonval


@pytest.fixture
def client(monkeypatch):
    """Install a FakeClient as the module's live client and hand it back."""
    fake = FakeClient()
    monkeypatch.setattr(mcp_server, "_CLIENT", fake)
    return fake


# --- tool -> command-line translation -------------------------------------

def test_power_on_off(client):
    run(mcp_server.power("t1", False))
    run(mcp_server.power("all", True))
    assert client.commands == ["t1 power off", "all power on"]


def test_set_hsi(client):
    run(mcp_server.set_hsi("all", 240, 100, 80))
    assert client.commands == ["all hsi 240 100 80"]


def test_set_cct_default_gm(client):
    run(mcp_server.set_cct("t1", 50, 56))
    run(mcp_server.set_cct("t1", 50, 56, 30))
    assert client.commands == ["t1 cct 50 56 50", "t1 cct 50 56 30"]


def test_set_rgbcw_defaults_and_full(client):
    run(mcp_server.set_rgbcw("t1", 40))
    run(mcp_server.set_rgbcw("all", 50, 0, 127, 250, 0, 0))
    assert client.commands == ["t1 rgbcw 40 0 0 0 0 0",
                               "all rgbcw 50 0 127 250 0 0"]


def test_set_xy(client):
    run(mcp_server.set_xy("t1", 50, 0.3127, 0.329))
    assert client.commands == ["t1 xy 50 0.3127 0.329"]


def test_set_gel_default_brand_and_named(client):
    run(mcp_server.set_gel("t1", 45, 100, 50))
    run(mcp_server.set_gel("all", 200, 90, 60, "lee", 7))
    assert client.commands == ["t1 gel 45 100 50 rosco 0",
                               "all gel 200 90 60 lee 7"]


def test_set_brightness(client):
    run(mcp_server.set_brightness("all", 80))
    assert client.commands == ["all bri 80"]


def test_scene_with_and_without_params(client):
    run(mcp_server.scene("all", 3))
    run(mcp_server.scene("all", 3, [9, 10]))
    assert client.commands == ["all scene 3", "all scene 3 9 10"]


def test_start_flow_with_and_without_opts(client):
    run(mcp_server.start_flow("palette"))
    run(mcp_server.start_flow("palette", {"speed": "0.05"}))
    assert client.commands == ["flow palette", "flow palette speed=0.05"]


def test_stop_and_query(client):
    run(mcp_server.stop())
    run(mcp_server.query_status("t2"))
    assert client.commands == ["stop", "query t2"]


def test_set_pixel(client):
    run(mcp_server.set_pixel("t1", ["0", "off", "240"]))
    assert client.commands == ["t1 pixel 0 off 240"]


def test_run_preset(client):
    run(mcp_server.run_preset("recording"))
    assert client.commands == ["preset recording"]


def test_get_state_parses_json_reply(monkeypatch):
    snap = {"AA": {"name": "NW-AA", "pos": 1, "connected": True, "power": "on"}}
    fake = FakeClient(result=json.dumps(snap))
    monkeypatch.setattr(mcp_server, "_CLIENT", fake)
    assert run(mcp_server.get_state("t1")) == snap
    assert fake.commands == ["state t1"]


def test_list_lights_reshapes_snapshot(monkeypatch):
    snap = {
        "AA": {"name": "NW-AA", "pos": 1, "connected": True},
        "BB": {"name": "NW-BB", "pos": None, "connected": False},
    }
    fake = FakeClient(jsonval=snap)
    monkeypatch.setattr(mcp_server, "_CLIENT", fake)
    lights = run(mcp_server.list_lights())
    assert fake.gets == ["/api/v1/state"]
    assert lights == [
        {"target": "t1", "mac": "AA", "name": "NW-AA", "position": 1, "connected": True},
        {"target": "BB", "mac": "BB", "name": "NW-BB", "position": None, "connected": False},
    ]


def test_list_presets(monkeypatch):
    presets = {"recording": ["all cct 90 48 50"]}
    fake = FakeClient(jsonval=presets)
    monkeypatch.setattr(mcp_server, "_CLIENT", fake)
    assert run(mcp_server.list_presets()) == presets
    assert fake.gets == ["/api/v1/presets"]


def test_tool_propagates_daemon_error(monkeypatch):
    fake = FakeClient(result=DaemonError("no tubes for target 't9'"))
    monkeypatch.setattr(mcp_server, "_CLIENT", fake)
    with pytest.raises(DaemonError) as exc:
        run(mcp_server.power("t9", True))
    assert "no tubes" in str(exc.value)


def test_uninitialised_client_raises():
    # No _CLIENT set -> a tool call surfaces a clear error, not an AttributeError.
    mcp_server._CLIENT = None
    with pytest.raises(DaemonError):
        run(mcp_server.stop())


# DaemonClient transport/status-mapping tests moved to test_client.py (the client
# is its own module now). This file covers the MCP tools + translation only.


# --- config resolution ----------------------------------------------------

def test_resolve_http_url_precedence(monkeypatch):
    monkeypatch.delenv("NEEWER_MCP_URL", raising=False)
    parser = mcp_server.build_parser()

    args = parser.parse_args(["--http-url", "http://cli:1"])
    assert mcp_server.resolve_http_url(args) == "http://cli:1"

    args = parser.parse_args([])
    monkeypatch.setenv("NEEWER_MCP_URL", "http://env:2")
    assert mcp_server.resolve_http_url(args) == "http://env:2"

    monkeypatch.delenv("NEEWER_MCP_URL")
    assert mcp_server.resolve_http_url(args) == mcp_server.DEFAULT_HTTP_URL


# --- MCP wiring (only when the optional SDK is present) -------------------

def test_build_app_registers_all_tools():
    pytest.importorskip("mcp")
    app = mcp_server.build_app(DaemonClient("http://x:8099"))
    # every tool in TOOLS should be registered by name
    tool_names = {t.name for t in run(app.list_tools())}
    for fn in mcp_server.TOOLS:
        assert fn.__name__ in tool_names


def test_tool_signatures_match_command_registry():
    """The MCP tool signatures are the third place the argument order appears (after
    the command dataclasses and the HTTP field-map). Pin them to the single source
    of truth (``commands.ACTIONS``) so a field reorder can't silently desync the
    tools an assistant calls."""
    import inspect

    from neewer.protocol import commands

    # MCP tool function -> grammar action it exposes.
    tool_action = {
        mcp_server.power: "power",
        mcp_server.set_hsi: "hsi",
        mcp_server.set_cct: "cct",
        mcp_server.set_brightness: "bri",
        mcp_server.set_rgbcw: "rgbcw",
        mcp_server.set_xy: "xy",
        mcp_server.set_gel: "gel",
        mcp_server.scene: "scene",
        mcp_server.set_pixel: "pixel",
    }
    for tool, action in tool_action.items():
        spec = commands.ACTIONS[action]
        params = list(inspect.signature(tool).parameters)
        assert params[0] == "target"
        expected = list(spec.fields) + ([spec.variadic] if spec.variadic else [])
        assert params[1:] == expected, action
