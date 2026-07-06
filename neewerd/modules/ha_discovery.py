"""Pure helpers for the Home Assistant MQTT-Discovery bridge (see ``mqtt.py``).

No I/O lives here: these build the retained discovery-config payloads and translate
between HA's JSON *light* schema and the neewer command grammar, so they unit-test
without a broker or a radio (mirrors how ``http.py`` keeps its arg helpers pure).

Two hard facts shape the design:

* The hardware reports **no colour/brightness** (``replies.py`` decodes only
  power/battery/mode/version/temp), so the HA state we publish is *optimistic* —
  reconstructed from the commands we send, overlaid with the one hardware-verified
  field (power). :func:`assumed_from_last` re-seeds that optimistic state from the
  tube's stored ``last`` command line so HA sliders survive a daemon restart.
* HA never sends ``color_mode`` on a command; it sends ``color`` *or* ``color_temp``
  and the receiver infers the mode. :func:`ha_set_to_lines` keys off which field is
  present, never an inbound ``color_mode``.
"""
from __future__ import annotations

from neewer.protocol import frames

#: Identifier for the synthetic "bridge" HA device that the group/``all`` entities
#: attach to, and that per-tube entities reference via ``via_device``. At least one
#: entity (``all``) always carries this in its ``device.identifiers``, so the
#: ``via_device`` link always resolves (HA would otherwise log "device not found").
BRIDGE_ID = "neewerd_bridge"

#: Hardware colour-temperature range, in kelvin (CCT is stored as hundreds of K).
MIN_KELVIN = frames.CCT_MIN * 100      # 3200
MAX_KELVIN = frames.CCT_MAX * 100      # 8500


# ---- ids ----------------------------------------------------------------
def _mac_slug(mac: str) -> str:
    """MAC -> a stable, topic-safe slug: ``CC:8D:..`` -> ``cc8d..``."""
    return mac.replace(":", "").replace("-", "").lower()


def object_id_tube(mac: str, node_id: str = "neewer") -> str:
    return f"{node_id}_{_mac_slug(mac)}"


def object_id_all(node_id: str = "neewer") -> str:
    return f"{node_id}_all"


def object_id_group(name: str, node_id: str = "neewer") -> str:
    return f"{node_id}_group_{name.lower()}"


# ---- kelvin / mireds ----------------------------------------------------
def kelvin_to_temp(kelvin: int) -> int:
    """Kelvin -> the grammar's hundreds-of-K CCT byte, clamped to hardware range."""
    return frames.clamp(round(kelvin / 100), frames.CCT_MIN, frames.CCT_MAX)


def mireds_to_kelvin(mireds: int) -> int:
    return round(1_000_000 / mireds)


def kelvin_to_mireds(kelvin: int) -> int:
    return round(1_000_000 / kelvin)


# ---- discovery payloads -------------------------------------------------
def _light_common(color_temp_unit: str) -> dict:
    """Fields shared by every light entity (colour modes, brightness scale, CCT)."""
    common = {
        "schema": "json",
        "brightness": True,
        "brightness_scale": 100,                 # HA uses our native 0-100, no rescale
        "supported_color_modes": ["color_temp", "hs"],
    }
    if color_temp_unit == "mireds":
        # older HA: color_temp in mireds. min mireds <-> max kelvin. Floor the min
        # and ceil the max so both kelvin endpoints stay reachable (3200 K == 312.5
        # mireds must round *up* to 313, not down).
        common["min_mireds"] = 1_000_000 // MAX_KELVIN         # floor(117.6) = 117
        common["max_mireds"] = -(-1_000_000 // MIN_KELVIN)     # ceil(312.5) = 313
    else:
        common["color_temp_kelvin"] = True                    # modern HA (2023.10+)
        common["min_kelvin"] = MIN_KELVIN
        common["max_kelvin"] = MAX_KELVIN
    return common


def tube_discovery(object_id, name, base, bridge_avail, color_temp_unit="kelvin") -> dict:
    """Discovery config for one physical tube (its own HA device, linked to the bridge)."""
    tube_avail = f"{base}/light/{object_id}/availability"
    payload = {
        "name": name,
        "unique_id": object_id,
        "object_id": object_id,
        "command_topic": f"{base}/light/{object_id}/set",
        "state_topic": f"{base}/light/{object_id}/state",
        "availability": [{"topic": bridge_avail}, {"topic": tube_avail}],
        "availability_mode": "all",
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": {
            "identifiers": [object_id],
            "name": name,
            "manufacturer": "Neewer",
            "model": "Infinity Tube (TL90C/TL120C)",
            "via_device": BRIDGE_ID,
        },
    }
    payload.update(_light_common(color_temp_unit))
    return payload


def group_discovery(object_id, name, base, bridge_avail, color_temp_unit="kelvin") -> dict:
    """Discovery config for a group / ``all`` entity (attached to the bridge device).

    A group has no single BLE link, so it carries only the bridge availability and
    lives on the bridge device (whose ``identifiers`` include :data:`BRIDGE_ID`,
    which is what per-tube ``via_device`` points at)."""
    payload = {
        "name": name,
        "unique_id": object_id,
        "object_id": object_id,
        "command_topic": f"{base}/light/{object_id}/set",
        "state_topic": f"{base}/light/{object_id}/state",
        "availability": [{"topic": bridge_avail}],
        "availability_mode": "all",
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": {
            "identifiers": [BRIDGE_ID],
            "name": "neewerd bridge",
            "manufacturer": "Neewer",
            "model": "neewerd",
        },
    }
    payload.update(_light_common(color_temp_unit))
    return payload


# ---- incoming: HA set JSON -> command lines -----------------------------
def _as_int(value, default=None):
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def ha_set_to_lines(target, payload, assumed, color_temp_unit="kelvin"):
    """Translate one HA ``set`` payload into command lines + the new assumed state.

    ``target`` is the resolved grammar target (``all`` / ``t<N>`` / group / MAC).
    ``assumed`` is this entity's last-known optimistic state; a copy is returned
    updated. HA sends *partial* payloads (e.g. brightness only), so we merge.

    Returns ``(lines, new_assumed)``. A leading ``power on`` precedes a colour/CCT
    change so a set-from-off lights up; whether that extra write is necessary is a
    hardware question (see issue #17 / D-5) — kept for safety.
    """
    assumed = dict(assumed)
    lines: list[str] = []

    if payload.get("state") == "OFF":
        assumed["power"] = "off"
        return [f"{target} power off"], assumed

    bri = _as_int(payload.get("brightness"))

    if "color" in payload:
        hue = (_as_int(payload["color"].get("h"), 0)) % 360
        sat = _as_int(payload["color"].get("s"), 0)
        level = bri if bri is not None else assumed.get("brightness", 100)
        lines += [f"{target} power on", f"{target} hsi {hue} {sat} {level}"]
        assumed.update(power="on", color_mode="hs", h=hue, s=sat, brightness=level)

    elif "color_temp" in payload:
        raw = _as_int(payload["color_temp"], MIN_KELVIN)
        kelvin = mireds_to_kelvin(raw) if color_temp_unit == "mireds" else raw
        temp = kelvin_to_temp(kelvin)
        level = bri if bri is not None else assumed.get("brightness", 100)
        lines += [f"{target} power on", f"{target} cct {level} {temp}"]
        assumed.update(power="on", color_mode="color_temp", temp=temp, brightness=level)

    elif bri is not None:
        # brightness-only: re-emit the assumed mode at the new level.
        mode = assumed.get("color_mode")
        if mode == "color_temp":
            temp = assumed.get("temp", frames.CCT_MIN)
            lines.append(f"{target} cct {bri} {temp}")
        elif mode == "hs":
            hue, sat = assumed.get("h", 0), assumed.get("s", 100)
            lines.append(f"{target} hsi {hue} {sat} {bri}")
        else:
            lines.append(f"{target} bri {bri}")     # no prior colour -> neutral white (lossy)
        assumed.update(power="on", brightness=bri)

    elif payload.get("state") == "ON":
        lines.append(f"{target} power on")
        assumed["power"] = "on"

    return lines, assumed


# ---- outgoing: assumed state (+ verified power) -> HA state JSON ---------
def snapshot_to_ha_state(assumed, snap_tube, color_temp_unit="kelvin"):
    """Build the HA state JSON for one entity from its assumed state, overlaying the
    one hardware-verified field (power) from the tube snapshot when present."""
    state: dict = {}
    power = (snap_tube or {}).get("power") or assumed.get("power")
    if power:
        state["state"] = "ON" if power == "on" else "OFF"
    if "brightness" in assumed:
        state["brightness"] = assumed["brightness"]
    mode = assumed.get("color_mode")
    if mode == "hs" and "h" in assumed:
        state["color_mode"] = "hs"
        state["color"] = {"h": assumed["h"], "s": assumed.get("s", 100)}
    elif mode == "color_temp" and "temp" in assumed:
        state["color_mode"] = "color_temp"
        kelvin = assumed["temp"] * 100
        state["color_temp"] = kelvin_to_mireds(kelvin) if color_temp_unit == "mireds" else kelvin
    return state


# ---- diagnostic sensors -------------------------------------------------
#: Diagnostic sensor entities announced per tube. Each reads one field from the
#: tube's retained ``.../attributes`` JSON via a value_template. Battery only has a
#: value on battery power (mains fixtures report power_source=external instead);
#: temp_c only when the fixture reports temperature.
SENSOR_SPECS = [
    {"key": "battery", "name": "Battery", "device_class": "battery", "unit": "%"},
    {"key": "power_source", "name": "Power source"},
    {"key": "version", "name": "Firmware"},
    {"key": "temp_c", "name": "Temperature", "device_class": "temperature", "unit": "°C"},
]


def sensor_discovery(tube_object_id, tube_name, base, bridge_avail):
    """Discovery configs for the per-tube diagnostic sensors.

    Returns ``[(discovery_object_id, payload), ...]``. Every sensor reads the
    tube's retained ``<base>/light/<id>/attributes`` JSON (published by the
    reconcile loop) and attaches to the tube's HA device so it groups under it.
    """
    attr_topic = f"{base}/light/{tube_object_id}/attributes"
    tube_avail = f"{base}/light/{tube_object_id}/availability"
    out = []
    for spec in SENSOR_SPECS:
        disc_id = f"{tube_object_id}_{spec['key']}"
        payload = {
            "name": spec["name"],
            "unique_id": disc_id,
            "object_id": disc_id,
            "state_topic": attr_topic,
            "value_template": "{{ value_json.%s | default('') }}" % spec["key"],
            "entity_category": "diagnostic",
            "availability": [{"topic": bridge_avail}, {"topic": tube_avail}],
            "availability_mode": "all",
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": {"identifiers": [tube_object_id]},
        }
        if "device_class" in spec:
            payload["device_class"] = spec["device_class"]
        if "unit" in spec:
            payload["unit_of_measurement"] = spec["unit"]
        out.append((disc_id, payload))
    return out


#: Diagnostic sensors on the bridge device itself (daemon-level telemetry). They
#: read the retained ``<base>/bridge/attributes`` JSON.
BRIDGE_SENSOR_SPECS = [
    {"key": "version", "name": "neewerd version"},
    {"key": "uptime_s", "name": "Uptime", "device_class": "duration", "unit": "s"},
    {"key": "lights_total", "name": "Lights known"},
    {"key": "lights_online", "name": "Lights online"},
    {"key": "lights_offline", "name": "Lights offline"},
]


def bridge_sensor_discovery(base, bridge_avail):
    """Discovery configs for the bridge-device telemetry sensors.

    Returns ``[(discovery_object_id, payload), ...]``. They attach to the same
    synthetic bridge device the group/``all`` light entities live on, and read the
    retained ``<base>/bridge/attributes`` JSON."""
    attr_topic = f"{base}/bridge/attributes"
    out = []
    for spec in BRIDGE_SENSOR_SPECS:
        disc_id = f"{BRIDGE_ID}_{spec['key']}"
        payload = {
            "name": spec["name"],
            "unique_id": disc_id,
            "object_id": disc_id,
            "state_topic": attr_topic,
            "value_template": "{{ value_json.%s | default('') }}" % spec["key"],
            "entity_category": "diagnostic",
            "availability": [{"topic": bridge_avail}],
            "availability_mode": "all",
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": {"identifiers": [BRIDGE_ID], "name": "neewerd bridge",
                       "manufacturer": "Neewer", "model": "neewerd"},
        }
        if "device_class" in spec:
            payload["device_class"] = spec["device_class"]
        if "unit" in spec:
            payload["unit_of_measurement"] = spec["unit"]
        out.append((disc_id, payload))
    return out


def bridge_attributes(snap, version, uptime_s):
    """Daemon telemetry for the bridge sensors: version, uptime, and light counts."""
    total = len(snap)
    online = sum(1 for t in snap.values() if t.get("connected"))
    return {
        "version": version,
        "uptime_s": int(uptime_s),
        "lights_total": total,
        "lights_online": online,
        "lights_offline": total - online,
    }


def tube_attributes(snap_tube):
    """The diagnostic-sensor values for one tube, from its snapshot state.

    Only includes fields the tube has actually reported (they populate after a
    ``query``). Derives ``power_source`` (``external`` mains flag, else ``battery``
    when a percentage is known)."""
    st = snap_tube or {}
    attrs: dict = {}
    if "battery" in st:
        attrs["battery"] = st["battery"]
    if st.get("power_source") == "external":
        attrs["power_source"] = "external"
    elif "battery" in st:
        attrs["power_source"] = "battery"
    if "version" in st:
        attrs["version"] = st["version"]
    if "temp_c" in st:
        attrs["temp_c"] = st["temp_c"]
    return attrs


def assumed_from_last(last_line):
    """Reconstruct optimistic assumed state from a stored ``last`` command line.

    ``dispatch`` records ``tube.state["last"]`` per MAC, which survives into
    ``snapshot()`` — so on restart we can recover the sliders' colour/brightness
    instead of showing blanks. Best-effort: an unparseable line yields ``{}``.
    """
    parts = (last_line or "").split()
    if len(parts) < 2:
        return {}
    action, args = parts[1], parts[2:]
    a: dict = {}
    if action == "power":
        a["power"] = "on" if args and args[0] in ("on", "1", "true") else "off"
    elif action == "hsi" and args:
        a.update(power="on", color_mode="hs", h=_as_int(args[0], 0))
        if len(args) >= 2:
            a["s"] = _as_int(args[1], 100)
        if len(args) >= 3:
            a["brightness"] = _as_int(args[2], 100)
    elif action == "cct" and len(args) >= 2:
        a.update(power="on", color_mode="color_temp",
                 brightness=_as_int(args[0], 100), temp=_as_int(args[1], frames.CCT_MIN))
    elif action == "bri" and args:
        a.update(power="on", brightness=_as_int(args[0], 100))
    return a
