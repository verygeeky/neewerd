"""Tests for :mod:`neewerd.modules.mqtt` — the HA-Discovery bridge orchestration.

``aiomqtt`` is an optional extra and isn't installed in CI, so we register a tiny
stub before importing the module (mirroring how conftest stubs ``bleak``). The
bridge's decisions are pure (they return publish-tuples), so no real broker runs.
"""
from __future__ import annotations

import json
import sys
import types

# --- stub aiomqtt so `import aiomqtt` in the module resolves ---------------
# Only when the real package is absent (CI): if aiomqtt IS installed (dev / a live
# run), leave it alone so the live tests get the real client, not this stub. The
# dry tests below never instantiate the client, so the real module is fine too.
try:
    import aiomqtt  # noqa: F401
except ImportError:
    _stub = types.ModuleType("aiomqtt")

    class _Will:
        def __init__(self, topic, payload=None, retain=False):
            self.topic, self.payload, self.retain = topic, payload, retain

    class _Client:  # never instantiated in these tests
        def __init__(self, *a, **k):
            pass

    _stub.Will = _Will
    _stub.Client = _Client
    sys.modules["aiomqtt"] = _stub

from neewerd.modules import mqtt  # noqa: E402


class FakeBook:
    def __init__(self, aliases=None, groups=None):
        self.aliases = aliases or {}
        self.groups = groups or {}


class FakeCore:
    def __init__(self, snap, book, resolve_map=None):
        self._snap = snap
        self.book = book
        self._resolve = resolve_map or {}
        self.dispatched = []

    def snapshot(self):
        return self._snap

    def resolve(self, target):
        return list(self._resolve.get(target, []))


MAC = "AA:BB:CC:DD:EE:01"
TUBE_OID = "neewer_aabbccddee01"


def _bridge(snap, book=None, resolve_map=None):
    core = FakeCore(snap, book or FakeBook(), resolve_map)
    return mqtt._Bridge(core, "neewer", "neewer", "kelvin")


# --- discovery announce / removal -----------------------------------------

def test_sync_discovery_announces_all_group_and_tube():
    snap = {MAC: {"name": "NW-x", "pos": 1, "connected": True}}
    b = _bridge(snap, FakeBook(aliases={"key": MAC}, groups={"keys": [MAC]}))
    pubs = b.sync_discovery(snap, "homeassistant")
    topics = {t for t, _, _ in pubs}
    assert "homeassistant/light/neewer_all/config" in topics
    assert "homeassistant/light/neewer_group_keys/config" in topics
    assert f"homeassistant/light/{TUBE_OID}/config" in topics
    # tube name resolves to the device-book alias
    tube_cfg = next(json.loads(p) for t, p, _ in pubs if t.endswith(f"{TUBE_OID}/config"))
    assert tube_cfg["name"] == "key"
    # all retained
    assert all(retain for _, _, retain in pubs)


def test_sync_discovery_is_idempotent():
    snap = {MAC: {"connected": True}}
    b = _bridge(snap)
    b.sync_discovery(snap, "homeassistant")
    assert b.sync_discovery(snap, "homeassistant") == []      # nothing new second time


def test_sync_discovery_removes_dropped_group_with_empty_payload():
    snap = {MAC: {"connected": True}}
    book = FakeBook(groups={"keys": [MAC]})
    core = FakeCore(snap, book)
    b = mqtt._Bridge(core, "neewer", "neewer", "kelvin")
    b.sync_discovery(snap, "homeassistant")
    assert "neewer_group_keys" in b.announced
    # drop the group from the book -> next sync publishes an empty retained config
    book.groups = {}
    pubs = b.sync_discovery(snap, "homeassistant")
    assert ("homeassistant/light/neewer_group_keys/config", "", True) in pubs
    assert "neewer_group_keys" not in b.announced


def test_sync_discovery_seeds_assumed_from_last():
    snap = {MAC: {"connected": True, "last": "all hsi 200 100 60"}}
    b = _bridge(snap)
    b.sync_discovery(snap, "homeassistant")
    assert b.assumed[TUBE_OID] == {
        "power": "on", "color_mode": "hs", "h": 200, "s": 100, "brightness": 60}


# --- availability + state (change-only) ------------------------------------

def test_reconcile_publishes_availability_on_change_only():
    snap = {MAC: {"connected": True}}
    b = _bridge(snap)
    b.sync_discovery(snap, "homeassistant")
    first = b.reconcile(snap)
    assert (f"neewer/light/{TUBE_OID}/availability", "online", True) in first
    assert b.reconcile(snap) == []                            # unchanged -> silent
    snap[MAC]["connected"] = False
    pubs = b.reconcile(snap)
    assert (f"neewer/light/{TUBE_OID}/availability", "offline", True) in pubs


def test_reconcile_publishes_state_from_assumed_and_power():
    snap = {MAC: {"connected": True, "power": "on"}}
    b = _bridge(snap)
    b.sync_discovery(snap, "homeassistant")
    b.assumed[TUBE_OID] = {"color_mode": "hs", "h": 10, "s": 50, "brightness": 70}
    pubs = b.reconcile(snap)
    state = next(json.loads(p) for t, p, _ in pubs if t.endswith(f"{TUBE_OID}/state"))
    assert state["state"] == "ON" and state["brightness"] == 70
    assert state["color"] == {"h": 10, "s": 50}


# --- incoming set ----------------------------------------------------------

def test_handle_set_returns_lines_and_updates_assumed():
    snap = {MAC: {"connected": True}}
    b = _bridge(snap)
    b.sync_discovery(snap, "homeassistant")
    lines = b.handle_set(f"neewer/light/{TUBE_OID}/set",
                         json.dumps({"state": "ON", "color": {"h": 240, "s": 100}}).encode())
    assert lines == [f"{MAC} power on", f"{MAC} hsi 240 100 100"]
    assert b.assumed[TUBE_OID]["h"] == 240


def test_handle_set_group_mirrors_to_member_tubes():
    snap = {MAC: {"connected": True}}
    b = _bridge(snap, FakeBook(groups={"keys": [MAC]}), resolve_map={"keys": [MAC]})
    b.sync_discovery(snap, "homeassistant")
    b.handle_set("neewer/light/neewer_group_keys/set",
                 json.dumps({"color_temp": 5000, "brightness": 90}).encode())
    # the member tube entity's assumed state now reflects the group command
    assert b.assumed[TUBE_OID]["color_mode"] == "color_temp"
    assert b.assumed[TUBE_OID]["brightness"] == 90


def test_handle_set_ignores_unknown_topic_and_bad_json():
    snap = {MAC: {"connected": True}}
    b = _bridge(snap)
    b.sync_discovery(snap, "homeassistant")
    assert b.handle_set("neewer/light/ghost/set", b"{}") == []
    assert b.handle_set(f"neewer/light/{TUBE_OID}/set", b"not json") == []


# --- diagnostic sensors + bridge telemetry --------------------------------

def test_sync_discovery_announces_tube_and_bridge_sensors():
    snap = {MAC: {"connected": True}}
    b = _bridge(snap)
    topics = {t for t, _, _ in b.sync_discovery(snap, "homeassistant")}
    # per-tube diagnostic sensors
    assert f"homeassistant/sensor/{TUBE_OID}_battery/config" in topics
    assert f"homeassistant/sensor/{TUBE_OID}_power_source/config" in topics
    # bridge telemetry sensors
    assert "homeassistant/sensor/neewerd_bridge_uptime_s/config" in topics
    assert "homeassistant/sensor/neewerd_bridge_lights_online/config" in topics


def test_reconcile_publishes_tube_attributes_on_change():
    snap = {MAC: {"connected": True, "power_source": "external", "version": "2.0.5"}}
    b = _bridge(snap)
    b.sync_discovery(snap, "homeassistant")
    pubs = b.reconcile(snap)
    attr = next((p for t, p, _ in pubs if t == f"neewer/light/{TUBE_OID}/attributes"), None)
    assert attr is not None
    assert json.loads(attr) == {"power_source": "external", "version": "2.0.5"}
    # unchanged -> not republished
    assert not any(t.endswith("/attributes") for t, _, _ in b.reconcile(snap))


def test_bridge_attributes_pub():
    snap = {MAC: {"connected": True}}
    b = _bridge(snap)
    topic, payload, retain = b.bridge_attributes_pub(snap, "0.1.0", 42.0)
    assert topic == "neewer/bridge/attributes" and retain is True
    assert json.loads(payload)["lights_online"] == 1


def test_removing_tube_nulls_its_sensor_configs():
    snap = {MAC: {"connected": True}}
    b = _bridge(snap)
    b.sync_discovery(snap, "homeassistant")
    # tube disappears from the roster -> its light + sensor configs get emptied
    pubs = b.sync_discovery({}, "homeassistant")
    nulled = {t for t, p, _ in pubs if p == ""}
    assert f"homeassistant/light/{TUBE_OID}/config" in nulled
    assert f"homeassistant/sensor/{TUBE_OID}_battery/config" in nulled


def test_object_id_from_set():
    b = _bridge({})
    assert b.object_id_from_set("neewer/light/neewer_all/set") == "neewer_all"
    assert b.object_id_from_set("neewer/light/x/state") is None
    assert b.object_id_from_set("other/topic") is None
