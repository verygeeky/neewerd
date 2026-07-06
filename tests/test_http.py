"""Tests for :mod:`neewerd.modules.http` — the REST front-end.

These exercise the routing/translation layer directly (``_route``) against a
fake core, so no socket is opened and no radio is touched. The fake core records
every dispatched command line and returns canned replies, letting us assert both
the translation (URL/JSON -> command line) and the HTTP status mapping.
"""
from __future__ import annotations

import asyncio
import json
import types

import pytest
from neewer.errors import UnknownEffect, UnknownPreset, UnknownTarget, Unsupported

from neewerd.modules import http


def run(coro):
    """Run a coroutine to completion on a fresh event loop."""
    return asyncio.run(coro)


class FakeCore:
    """Stands in for NeewerCore: records dispatched lines, returns canned replies.

    ``reply`` may be a string (returned for every dispatch) or a callable taking
    the command line. A reply of an ``Exception`` instance is raised, so tests
    can simulate ``dispatch`` raising ``ValueError`` on bad args.
    """

    def __init__(self, reply="ok hsi -> 1 tube(s)", snap=None, presets=None):
        self.reply = reply
        self.snap = snap or {"AA": {"name": "NW-AA", "pos": 1, "connected": True}}
        self.dispatched: list[str] = []
        #: Presets are a daemon verb now; the /presets route reads the table off the
        #: registered runner. Mirror that shape with a stub runner exposing .presets.
        self.verbs = {"preset": types.SimpleNamespace(presets=presets or {})}

    async def dispatch(self, line: str):
        self.dispatched.append(line)
        result = self.reply(line) if callable(self.reply) else self.reply
        if isinstance(result, Exception):
            raise result
        return result

    def snapshot(self):
        return self.snap


def route(core, method, path, body=b""):
    """Run the http router and return (status, content_type, payload)."""
    return run(http._route(core, method, path, body))


# --- legacy line transport ------------------------------------------------

def test_post_cmd_body_is_dispatched_verbatim():
    core = FakeCore()
    status, ctype, payload = route(core, "POST", "/cmd", b"all hsi 240 100 80")
    assert core.dispatched == ["all hsi 240 100 80"]
    assert status == 200 and ctype == "text/plain"
    assert payload == "ok hsi -> 1 tube(s)"


def test_path_as_command_fallback():
    core = FakeCore(reply="ok power -> 1 tube(s)")
    status, _, _ = route(core, "GET", "/all/power/off")
    assert core.dispatched == ["all power off"]
    assert status == 200


def test_query_string_is_ignored():
    core = FakeCore()
    route(core, "GET", "/all/power/off?token=x")
    assert core.dispatched == ["all power off"]


def _strip_unit_fields(snap):
    """A snapshot with the always-added unit-id fields removed, for equality checks."""
    return {mac: {k: v for k, v in tube.items() if k not in ("unit", "unit_name")}
            for mac, tube in snap.items()}


def test_get_state_returns_json_snapshot():
    core = FakeCore()
    status, ctype, payload = route(core, "GET", "/state")
    assert status == 200 and ctype == "application/json"
    assert _strip_unit_fields(json.loads(payload)) == core.snap
    assert core.dispatched == []                 # state bypasses dispatch


# --- /api/v1 translation --------------------------------------------------

def test_api_hsi_named_fields_become_ordered_args():
    core = FakeCore()
    status, ctype, payload = route(
        core, "POST", "/api/v1/all/hsi", b'{"h":240,"s":100,"i":80}')
    assert core.dispatched == ["all hsi 240 100 80"]
    assert status == 200 and ctype == "application/json"
    assert json.loads(payload) == {"result": "ok hsi -> 1 tube(s)"}


def test_api_cct_partial_fields_keep_order():
    core = FakeCore()
    route(core, "POST", "/api/v1/t1/cct", b'{"bri":50,"temp":5600}')
    assert core.dispatched == ["t1 cct 50 5600"]


def test_api_power_boolean_true():
    core = FakeCore(reply="ok power -> 1 tube(s)")
    route(core, "POST", "/api/v1/all/power", b'{"on":true}')
    assert core.dispatched == ["all power on"]


def test_api_power_boolean_false():
    core = FakeCore(reply="ok power -> 1 tube(s)")
    route(core, "POST", "/api/v1/all/power", b'{"on":false}')
    assert core.dispatched == ["all power off"]


def test_api_integral_float_renders_without_decimal():
    # JSON 240 may decode as float in some bodies; must not become "240.0".
    core = FakeCore()
    route(core, "POST", "/api/v1/all/hsi", b'{"h":240.0,"s":100.0,"i":80.0}')
    assert core.dispatched == ["all hsi 240 100 80"]


def test_api_args_array_override():
    core = FakeCore()
    route(core, "POST", "/api/v1/all/hsi", b'{"args":[1,2,3]}')
    assert core.dispatched == ["all hsi 1 2 3"]


def test_api_cmd_escape_hatch_ignores_path():
    core = FakeCore()
    route(core, "POST", "/api/v1/whatever", b'{"cmd":"t2 bri 40"}')
    assert core.dispatched == ["t2 bri 40"]


def test_api_no_body_targetless_verb():
    core = FakeCore(reply="ok stopped")
    route(core, "POST", "/api/v1/stop")
    assert core.dispatched == ["stop"]


def test_api_state_returns_json_snapshot():
    core = FakeCore()
    status, ctype, payload = route(core, "GET", "/api/v1/state")
    assert status == 200 and ctype == "application/json"
    assert _strip_unit_fields(json.loads(payload)) == core.snap


# --- governor telemetry enrichment (#46 console) --------------------------

class _StubGovernor:
    """Minimal stand-in for a WriteGovernor: exposes a canned ``stats()`` dict."""

    def __init__(self, **stats):
        self._stats = stats

    def stats(self):
        return dict(self._stats)


def test_state_enriched_with_governor_stats_per_tube():
    # Two tubes; only AA is patched to Art-Net (has a governor). The state payload
    # should fold that tube's stats() under "gov"; the un-governed tube gets none.
    snap = {
        "AA": {"name": "NW-AA", "pos": 1, "connected": True},
        "BB": {"name": "NW-BB", "pos": 2, "connected": True},
    }
    gov_stats = {"rate": 12.0, "bw": 15.0, "min_rtt": 0.03,
                 "mode": "CRUISE", "sent": 100, "deferred": 4}
    core = FakeCore(snap=snap)
    core.write_governors = {"AA": _StubGovernor(**gov_stats)}

    for path in ("/state", "/api/v1/state"):
        status, ctype, payload = route(core, "GET", path)
        assert status == 200 and ctype == "application/json"
        data = json.loads(payload)
        assert data["AA"]["gov"] == gov_stats
        assert set(data["AA"]["gov"]) == {
            "rate", "bw", "min_rtt", "mode", "sent", "deferred"}
        assert "gov" not in data["BB"]                # not patched -> no governor
        # Enrichment must not mutate the core's cached snapshot dicts.
        assert "gov" not in core.snap["AA"]


def test_state_without_write_governors_is_plain_snapshot():
    # No artnet module ran -> core has no write_governors -> no gov keys. Unit-id
    # fields are still folded in (they don't depend on the governors).
    core = FakeCore()
    status, _, payload = route(core, "GET", "/api/v1/state")
    assert status == 200
    assert _strip_unit_fields(json.loads(payload)) == core.snap
    assert all("gov" not in tube for tube in json.loads(payload).values())


# --- unit-id enrichment (config-driven unit name) -------------------------

def test_state_enriched_with_unit_and_unit_name():
    # AA advertises a networkId suffix and has a configured label; BB has a
    # suffix but no label; CC has no '&' suffix at all.
    from neewer.devices import DeviceBook

    snap = {
        "AA": {"name": "NW-20240012&00900002", "pos": 1, "connected": True},
        "BB": {"name": "NW-30&01200003", "pos": 2, "connected": True},
        "CC": {"name": "NW-plain", "pos": 3, "connected": True},
    }
    core = FakeCore(snap=snap)
    core.book = DeviceBook(units={"00900002": "Key Right"})

    for path in ("/state", "/api/v1/state"):
        data = json.loads(route(core, "GET", path)[2])
        # AA: raw netid parsed + configured label resolved
        assert data["AA"]["unit"] == "00900002"
        assert data["AA"]["unit_name"] == "Key Right"
        # BB: netid parsed, but no label configured -> None
        assert data["BB"]["unit"] == "01200003"
        assert data["BB"]["unit_name"] is None
        # CC: no '&' suffix -> both null, never an exception
        assert data["CC"]["unit"] is None
        assert data["CC"]["unit_name"] is None
        # Enrichment must not mutate the core's cached snapshot dicts.
        assert "unit" not in core.snap["AA"]


def test_state_unit_fields_null_without_book():
    # A core with no device book still gets unit parsed from the name, but
    # unit_name stays null (nothing to resolve against) and never raises.
    snap = {"AA": {"name": "NW-x&00900002", "pos": 1, "connected": True}}
    core = FakeCore(snap=snap)                    # no core.book attribute
    data = json.loads(route(core, "GET", "/api/v1/state")[2])
    assert data["AA"]["unit"] == "00900002"
    assert data["AA"]["unit_name"] is None


# --- status code mapping --------------------------------------------------

def test_no_tubes_maps_to_404():
    core = FakeCore(reply=UnknownTarget("t9"))
    status, _, payload = route(core, "POST", "/api/v1/t9/hsi", b'{"h":1,"s":2,"i":3}')
    assert status == 404
    assert json.loads(payload) == {"error": "no tubes for target 't9'"}


def test_unknown_effect_maps_to_400():
    core = FakeCore(reply=UnknownEffect("nope"))
    status, _, _ = route(core, "POST", "/api/v1/all/flow/nope")
    assert status == 400


def test_unsupported_maps_to_422():
    # A command every addressed fixture silently can't do (e.g. pixel on non-pixel
    # tubes) is well-formed but unprocessable -> 422 (not the old 200-with-detail).
    core = FakeCore(reply=Unsupported("pixel unsupported on target 'all' (2 non-pixel tube(s))"))
    status, _, payload = route(core, "POST", "/api/v1/all/pixel", b'{"colors":["0"]}')
    assert status == 422
    assert "unsupported" in json.loads(payload)["error"]


def test_bad_args_valueerror_maps_to_400():
    core = FakeCore(reply=ValueError("expected integer arguments"))
    status, _, payload = route(core, "POST", "/api/v1/all/hsi", b'{"args":["x"]}')
    assert status == 400
    assert "integer" in json.loads(payload)["error"]


def test_legacy_no_tubes_also_maps_to_404():
    core = FakeCore(reply=UnknownTarget("t9"))
    status, ctype, payload = route(core, "POST", "/cmd", b"t9 power on")
    assert status == 404 and ctype == "text/plain"
    assert payload == "no tubes for target 't9'"


def test_unexpected_exception_maps_to_500():
    core = FakeCore(reply=RuntimeError("radio on fire"))
    status, _, payload = route(core, "POST", "/cmd", b"all hsi 1 2 3")
    assert status == 500
    assert "radio on fire" in payload


# --- malformed requests ---------------------------------------------------

def test_invalid_json_body_maps_to_400():
    core = FakeCore()
    status, ctype, payload = route(core, "POST", "/api/v1/all/hsi", b"{not json")
    assert status == 400 and ctype == "application/json"
    assert "invalid JSON" in json.loads(payload)["error"]
    assert core.dispatched == []                 # never reached dispatch


def test_api_path_without_action_maps_to_400():
    core = FakeCore()
    status, _, payload = route(core, "POST", "/api/v1/all", b"")
    assert status == 400
    assert "error" in json.loads(payload)
    assert core.dispatched == []


def test_empty_api_path_maps_to_400():
    core = FakeCore()
    status, _, _ = route(core, "GET", "/api/v1/")
    assert status == 400


# --- response builder -----------------------------------------------------

def test_response_content_length_is_byte_accurate_for_non_ascii():
    raw = http._response("café", 200, "text/plain")
    # The body is 5 bytes (é is two bytes in UTF-8), not 4 characters.
    assert b"Content-Length: 5\r\n" in raw
    assert raw.endswith("café".encode())


def test_response_includes_reason_phrase():
    assert b"HTTP/1.1 404 Not Found\r\n" in http._response("x", 404)
    assert b"HTTP/1.1 400 Bad Request\r\n" in http._response("x", 400)


# --- bundled web UI -------------------------------------------------------

def test_root_serves_ui_html():
    core = FakeCore()
    status, ctype, payload = route(core, "GET", "/")
    assert status == 200 and ctype.startswith("text/html")
    assert "<html" in payload.lower() or "<!doctype" in payload.lower()
    assert core.dispatched == []                 # UI never hits dispatch


def test_ui_aliases_serve_html():
    core = FakeCore()
    for path in ("/ui", "/index.html", "/ui/"):
        status, ctype, _ = route(core, "GET", path)
        assert status == 200 and ctype.startswith("text/html"), path


def test_post_root_is_not_the_ui():
    # The UI is GET-only; POST / falls through to the command path (and errors).
    core = FakeCore(reply=UnknownTarget(""))
    status, ctype, _ = route(core, "POST", "/", b"")
    assert ctype == "text/plain"                 # not the HTML page


# --- presets (config-defined) ---------------------------------------------

def test_get_presets_lists_names_and_lines():
    presets = {"recording": ["all cct 90 48 50"], "off": ["all power off"]}
    core = FakeCore(presets=presets)
    status, ctype, payload = route(core, "GET", "/api/v1/presets")
    assert status == 200 and ctype == "application/json"
    assert json.loads(payload) == presets
    assert core.dispatched == []                 # discovery never dispatches


def test_get_presets_trailing_slash():
    core = FakeCore(presets={"x": ["all power off"]})
    status, _, _ = route(core, "GET", "/api/v1/presets/")
    assert status == 200


def test_post_preset_dispatches_preset_line():
    core = FakeCore(reply="ok preset 'recording': ok cct -> 1 tube(s)")
    status, ctype, payload = route(core, "POST", "/api/v1/preset/recording", b"")
    assert core.dispatched == ["preset recording"]
    assert status == 200 and ctype == "application/json"
    assert json.loads(payload)["result"].startswith("ok preset 'recording'")


def test_post_preset_unknown_is_404():
    core = FakeCore(reply=UnknownPreset("ghost"))
    status, _, payload = route(core, "POST", "/api/v1/preset/ghost", b"")
    assert status == 404
    assert json.loads(payload)["error"] == "no preset 'ghost'"


# --- catalogue + target discovery (UI-parameter-exposure Phase 1) ----------

def test_get_catalog_returns_the_library_catalogue():
    from neewer import catalog

    core = FakeCore()
    status, ctype, payload = route(core, "GET", "/api/v1/catalog")
    assert status == 200 and ctype == "application/json"
    data = json.loads(payload)
    # The blob is the library catalogue served verbatim (JSON stringifies the
    # int scene/pixel/brand keys).
    assert data == json.loads(json.dumps(catalog.catalog()))
    assert set(data) >= {"version", "actions", "scenes", "scene_id_sets",
                         "pixel_effects", "flow_modes", "gel_brands"}
    assert core.dispatched == []                 # discovery never dispatches


def test_catalog_actions_derive_from_the_registry():
    """The served actions schema mirrors ``commands.ACTIONS`` — no local copy."""
    from neewer.protocol import commands

    data = json.loads(route(FakeCore(), "GET", "/api/v1/catalog")[2])
    for action, spec in commands.ACTIONS.items():
        assert data["actions"][action]["fields"] == list(spec.fields)
        assert data["actions"][action]["variadic"] == spec.variadic


def test_get_catalog_trailing_slash():
    assert route(FakeCore(), "GET", "/api/v1/catalog/")[0] == 200


def test_get_targets_folds_in_book_groups_and_aliases():
    from neewer.devices import DeviceBook

    caps = {"pixel": True, "scene_legacy": False}
    snap = {
        "AA": {"name": "NW-AA", "pos": 1, "connected": True,
               "model": "TL120C", "caps": caps},
        "BB": {"name": "NW-BB", "pos": None, "connected": False},
    }
    core = FakeCore(snap=snap)
    core.book = DeviceBook(aliases={"desk": "AA"}, groups={"key": ["desk", "BB"]})

    status, ctype, payload = route(core, "GET", "/api/v1/targets")
    assert status == 200 and ctype == "application/json"
    data = json.loads(payload)
    # Positioned tube -> t<pos>; unpositioned -> its MAC. caps ride along.
    assert data["tubes"]["AA"]["target"] == "t1"
    assert data["tubes"]["AA"]["model"] == "TL120C"
    assert data["tubes"]["AA"]["caps"] == caps
    assert data["tubes"]["BB"]["target"] == "BB"
    assert data["groups"] == {"key": ["desk", "BB"]}
    assert data["aliases"] == {"desk": "AA"}
    assert core.dispatched == []                 # discovery never dispatches


def test_get_targets_without_book_is_empty_groups_and_aliases():
    core = FakeCore()                            # no core.book attribute
    data = json.loads(route(core, "GET", "/api/v1/targets")[2])
    assert data["groups"] == {} and data["aliases"] == {}
    assert set(data["tubes"]) == set(core.snap)


def test_state_passes_caps_through_enrichment():
    # The library snapshot now carries per-tube caps; the enriched state payload
    # (and thus the SSE stream, which reuses it) must pass them through intact.
    caps = {"pixel": True, "scene_legacy": False, "scene_mac": True,
            "rgbcw": True, "xy": True, "gel": True, "streamer": False}
    snap = {"AA": {"name": "NW-AA", "pos": 1, "connected": True, "caps": caps}}
    core = FakeCore(snap=snap)
    data = json.loads(route(core, "GET", "/api/v1/state")[2])
    assert data["AA"]["caps"] == caps


# --- request-body cap (security #32) --------------------------------------

def test_oversized_content_length_is_rejected_413():
    head = b"POST /cmd HTTP/1.1\r\nContent-Length: 99999999\r\n\r\n"
    with pytest.raises(http._HttpError) as exc:
        run(http._read_body(None, head))            # cap trips before any read
    assert exc.value.status == 413


def test_normal_body_reads_through():
    async def body():
        reader = asyncio.StreamReader()             # must be created in the loop
        reader.feed_data(b"hello")
        reader.feed_eof()
        head = b"POST /cmd HTTP/1.1\r\nContent-Length: 5\r\n\r\n"
        return await http._read_body(reader, head)
    assert run(body()) == b"hello"


def test_no_content_length_is_empty_body():
    head = b"GET / HTTP/1.1\r\nHost: x\r\n\r\n"
    assert run(http._read_body(None, head)) == b""


# --- pixel (#33) ----------------------------------------------------------

def test_api_pixel_colors_field_becomes_args():
    core = FakeCore(reply="ok pixel -> 1 tube(s)")
    route(core, "POST", "/api/v1/t1/pixel", b'{"colors":["0","off","240"]}')
    assert core.dispatched == ["t1 pixel 0 off 240"]


# --- by-MAC colour modes: rgbcw / xy / gel (FORK A) -----------------------

def test_api_rgbcw_named_fields_become_ordered_args():
    core = FakeCore(reply="ok rgbcw -> 1 tube(s)")
    status, ctype, payload = route(
        core, "POST", "/api/v1/t1/rgbcw",
        b'{"bri":50,"r":0,"g":127,"b":250,"c":0,"w":0}')
    assert core.dispatched == ["t1 rgbcw 50 0 127 250 0 0"]
    assert status == 200 and ctype == "application/json"
    assert json.loads(payload) == {"result": "ok rgbcw -> 1 tube(s)"}


def test_api_xy_named_fields_keep_order():
    core = FakeCore(reply="ok xy -> 1 tube(s)")
    route(core, "POST", "/api/v1/t1/xy", b'{"bri":50,"x":0.3127,"y":0.329}')
    assert core.dispatched == ["t1 xy 50 0.3127 0.329"]


def test_api_gel_named_fields_with_brand_and_number():
    core = FakeCore(reply="ok gel -> 1 tube(s)")
    route(core, "POST", "/api/v1/t1/gel",
          b'{"hue":45,"sat":100,"bri":50,"brand":"rosco","gel_no":1}')
    assert core.dispatched == ["t1 gel 45 100 50 rosco 1"]


def test_api_gel_partial_fields_keep_order():
    # brand/gel_no omitted -> only the required three trailing args.
    core = FakeCore(reply="ok gel -> 1 tube(s)")
    route(core, "POST", "/api/v1/t1/gel", b'{"hue":45,"sat":100,"bri":50}')
    assert core.dispatched == ["t1 gel 45 100 50"]


def test_args_from_body_orders_by_registry():
    """The REST field-map derives its order from ``commands.ACTIONS``, not a local
    copy. Feed each scalar action a body keyed by its registry fields and assert the
    args come back in registry order (so a reorder there follows through here)."""
    from neewer.protocol import commands

    for action, spec in commands.ACTIONS.items():
        if action in ("power", "identify", "raw") or not spec.fields:
            continue                                   # boolean / no scalar fields
        body = {field: idx for idx, field in enumerate(spec.fields)}
        assert http._args_from_body(action, body) == [str(i) for i in range(len(spec.fields))]


def test_args_from_body_appends_variadic():
    """Scene params / pixel colours are appended after the scalars, per the registry."""
    assert http._args_from_body("scene", {"effect": 3, "params": [9, 1]}) == ["3", "9", "1"]
    assert http._args_from_body("pixel", {"colors": ["0", "240", "off"]}) == ["0", "240", "off"]
