"""Tests for :mod:`neewerd.modules.ha_discovery` — the pure HA<->grammar helpers.

No broker, no radio, no aiomqtt import (this module depends only on ``frames``).
"""
from __future__ import annotations

from neewer.protocol import frames

from neewerd.modules import ha_discovery as ha

MAC = "AA:BB:CC:DD:EE:01"
OID = "neewer_aabbccddee01"


# --- ids + conversions ----------------------------------------------------

def test_object_ids():
    assert ha.object_id_tube(MAC) == OID
    assert ha.object_id_all() == "neewer_all"
    assert ha.object_id_group("Keys") == "neewer_group_keys"
    assert ha.object_id_tube(MAC, node_id="lab") == "lab_aabbccddee01"


def test_kelvin_temp_roundtrips_and_clamps():
    assert ha.kelvin_to_temp(5600) == 56
    assert ha.kelvin_to_temp(1000) == frames.CCT_MIN     # clamp low
    assert ha.kelvin_to_temp(99999) == frames.CCT_MAX    # clamp high
    assert ha.mireds_to_kelvin(ha.kelvin_to_mireds(5000)) == 5000


# --- discovery payloads ---------------------------------------------------

def test_tube_discovery_shape_kelvin():
    p = ha.tube_discovery(OID, "key", "neewer", "neewer/bridge/availability")
    assert p["schema"] == "json"
    assert p["brightness_scale"] == 100
    assert p["supported_color_modes"] == ["color_temp", "hs"]
    assert p["color_temp_kelvin"] is True
    assert (p["min_kelvin"], p["max_kelvin"]) == (3200, 8500)
    assert p["command_topic"] == "neewer/light/neewer_aabbccddee01/set"
    assert p["state_topic"] == "neewer/light/neewer_aabbccddee01/state"
    assert {a["topic"] for a in p["availability"]} == {
        "neewer/bridge/availability", "neewer/light/neewer_aabbccddee01/availability"}
    assert p["device"]["identifiers"] == [OID]
    assert p["device"]["via_device"] == ha.BRIDGE_ID


def test_tube_discovery_mireds_variant():
    p = ha.tube_discovery(OID, "key", "neewer", "neewer/bridge/availability",
                          color_temp_unit="mireds")
    assert "color_temp_kelvin" not in p
    assert (p["min_mireds"], p["max_mireds"]) == (117, 313)


def test_group_discovery_attaches_to_bridge_device():
    p = ha.group_discovery("neewer_all", "All Tubes", "neewer", "neewer/bridge/availability")
    assert p["device"]["identifiers"] == [ha.BRIDGE_ID]      # so via_device resolves
    assert [a["topic"] for a in p["availability"]] == ["neewer/bridge/availability"]


# --- incoming: HA set -> lines --------------------------------------------

def test_set_off():
    lines, assumed = ha.ha_set_to_lines("all", {"state": "OFF"}, {})
    assert lines == ["all power off"] and assumed["power"] == "off"


def test_set_color_emits_power_then_hsi():
    lines, assumed = ha.ha_set_to_lines(
        "t1", {"state": "ON", "color": {"h": 240, "s": 100}, "brightness": 80}, {})
    assert lines == ["t1 power on", "t1 hsi 240 100 80"]
    assert assumed == {"power": "on", "color_mode": "hs", "h": 240, "s": 100, "brightness": 80}


def test_set_color_temp_kelvin_and_mireds():
    lines, _ = ha.ha_set_to_lines("all", {"color_temp": 5000, "brightness": 90}, {})
    assert lines == ["all power on", "all cct 90 50"]        # 5000K -> 50 (hundreds)
    lines_m, _ = ha.ha_set_to_lines("all", {"color_temp": 200}, {}, color_temp_unit="mireds")
    assert lines_m == ["all power on", "all cct 100 50"]     # 200 mired -> 5000K -> 50


def test_set_brightness_only_reuses_assumed_mode():
    # prior hs -> brightness-only re-emits hsi at the new level
    lines, _ = ha.ha_set_to_lines("t1", {"brightness": 30},
                                  {"color_mode": "hs", "h": 120, "s": 100, "brightness": 90})
    assert lines == ["t1 hsi 120 100 30"]
    # no prior colour -> neutral white fallback
    lines2, _ = ha.ha_set_to_lines("t1", {"brightness": 40}, {})
    assert lines2 == ["t1 bri 40"]


def test_set_hue_wraps_360():
    lines, _ = ha.ha_set_to_lines("t1", {"color": {"h": 360, "s": 100}}, {})
    assert lines[-1] == "t1 hsi 0 100 100"


# --- outgoing: assumed (+ power) -> HA state -------------------------------

def test_state_power_from_snapshot_overlays_assumed():
    st = ha.snapshot_to_ha_state({"color_mode": "hs", "h": 10, "s": 50, "brightness": 70},
                                 {"power": "on"})
    assert st["state"] == "ON"
    assert st["brightness"] == 70
    assert st["color_mode"] == "hs" and st["color"] == {"h": 10, "s": 50}


def test_state_color_temp_kelvin_and_mireds():
    a = {"color_mode": "color_temp", "temp": 50, "brightness": 80}
    assert ha.snapshot_to_ha_state(a, {})["color_temp"] == 5000
    assert ha.snapshot_to_ha_state(a, {}, color_temp_unit="mireds")["color_temp"] == 200


def test_state_empty_when_nothing_known():
    assert ha.snapshot_to_ha_state({}, {}) == {}


# --- restart seed ---------------------------------------------------------

# --- diagnostic sensors ---------------------------------------------------

def test_sensor_discovery_covers_specs_and_attaches_to_tube():
    sensors = ha.sensor_discovery(OID, "key", "neewer", "neewer/bridge/availability")
    ids = {sid for sid, _ in sensors}
    assert ids == {f"{OID}_{k}" for k in ("battery", "power_source", "version", "temp_c")}
    battery = next(p for sid, p in sensors if sid.endswith("_battery"))
    assert battery["device_class"] == "battery" and battery["unit_of_measurement"] == "%"
    assert battery["state_topic"] == f"neewer/light/{OID}/attributes"
    assert battery["entity_category"] == "diagnostic"
    assert battery["device"]["identifiers"] == [OID]      # groups under the tube device


def test_tube_attributes_external_power():
    st = {"power_source": "external", "version": "2.0.5", "mode": 2}
    assert ha.tube_attributes(st) == {"power_source": "external", "version": "2.0.5"}


def test_tube_attributes_on_battery():
    assert ha.tube_attributes({"battery": 77, "version": "1.1.11"}) == {
        "battery": 77, "power_source": "battery", "version": "1.1.11"}


def test_tube_attributes_empty_before_query():
    assert ha.tube_attributes({"name": "x", "connected": True}) == {}


# --- bridge telemetry -----------------------------------------------------

def test_bridge_sensor_discovery_on_bridge_device():
    sensors = ha.bridge_sensor_discovery("neewer", "neewer/bridge/availability")
    ids = {sid for sid, _ in sensors}
    assert ids == {f"{ha.BRIDGE_ID}_{k}" for k in
                   ("version", "uptime_s", "lights_total", "lights_online", "lights_offline")}
    uptime = next(p for sid, p in sensors if sid.endswith("_uptime_s"))
    assert uptime["device_class"] == "duration" and uptime["unit_of_measurement"] == "s"
    assert uptime["device"]["identifiers"] == [ha.BRIDGE_ID]
    assert uptime["state_topic"] == "neewer/bridge/attributes"


def test_bridge_attributes_counts():
    snap = {"AA": {"connected": True}, "BB": {"connected": False}, "CC": {"connected": True}}
    attrs = ha.bridge_attributes(snap, "0.1.0", 123.9)
    assert attrs == {"version": "0.1.0", "uptime_s": 123,
                     "lights_total": 3, "lights_online": 2, "lights_offline": 1}


def test_assumed_from_last_reconstructs_state():
    assert ha.assumed_from_last("all hsi 240 100 80") == {
        "power": "on", "color_mode": "hs", "h": 240, "s": 100, "brightness": 80}
    assert ha.assumed_from_last("t1 cct 90 48") == {
        "power": "on", "color_mode": "color_temp", "brightness": 90, "temp": 48}
    assert ha.assumed_from_last("all power off") == {"power": "off"}
    assert ha.assumed_from_last("") == {}
    assert ha.assumed_from_last("garbage") == {}
