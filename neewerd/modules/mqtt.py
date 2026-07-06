"""neewer2mqtt — MQTT bridge with Home Assistant MQTT-Discovery.

Two layers over one broker connection:

* **Home Assistant Discovery** (default on). Publishes a retained
  ``<discovery_prefix>/light/<object_id>/config`` per tube, per named group, and
  for ``all`` — so the tubes appear as real ``light`` entities with brightness /
  colour-temp / colour, no YAML. HA's light-JSON is translated to/from the command
  grammar by the pure helpers in :mod:`.ha_discovery`, so ``core.dispatch`` stays
  the single contract.

* **Legacy line transport** (kept for back-compat, toggle ``legacy_topics``).
  Subscribe ``<base>/cmd`` for raw command lines, publish a retained snapshot to
  ``<base>/state``::

      mosquitto_pub -t neewer/cmd -m 'all hsi 240 100 80'

Requires the optional ``aiomqtt`` dependency (``pip install '.[mqtt]'``). The whole
connection is wrapped in a reconnect loop so a broker restart is survivable; the
retained discovery configs are idempotent on re-announce.

State honesty: the hardware reports no colour/brightness, so per-entity HA state is
*optimistic* (reconstructed from the commands we send, seeded from each tube's
stored ``last`` line on restart) overlaid with the hardware-verified power. Retained
state is published **on change only** to avoid waking HA every interval for nothing;
availability tracks the BLE connection so a dropped tube greys out.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time

import aiomqtt

from .. import __version__
from . import ha_discovery as ha

log = logging.getLogger("neewerd.mqtt")

RECONNECT_DELAY = 5.0          # seconds to wait before retrying a lost broker


class _Bridge:
    """Per-connection HA-Discovery state + the pure publish/translate decisions.

    Methods return lists of ``(topic, payload, retain)`` tuples rather than doing
    I/O, so the whole bridge is testable without a broker; :func:`run` awaits the
    actual ``client.publish`` calls.
    """

    def __init__(self, core, base, node_id, color_temp_unit):
        self.core = core
        self.base = base
        self.node_id = node_id
        self.unit = color_temp_unit
        self.bridge_avail = f"{base}/bridge/availability"
        #: object_id -> (grammar target, kind, mac|None). kind in {"all","group","tube"}.
        self.meta: dict[str, tuple] = {}
        self.assumed: dict[str, dict] = {}       # object_id -> optimistic HA state
        self.announced: dict[str, str] = {}      # announce-key -> its retained config topic
        self._last_state: dict[str, str] = {}     # object_id -> last published state json
        self._last_avail: dict[str, str] = {}     # object_id -> last published availability
        self._last_attrs: dict[str, str] = {}     # object_id -> last published attributes json

    # ---- topic helpers ----
    def _config_topic(self, prefix, object_id):
        return f"{prefix}/light/{object_id}/config"

    def _avail_topic(self, object_id):
        return f"{self.base}/light/{object_id}/availability"

    def _state_topic(self, object_id):
        return f"{self.base}/light/{object_id}/state"

    def object_id_from_set(self, topic):
        """``<base>/light/<object_id>/set`` -> object_id, or None."""
        prefix, suffix = f"{self.base}/light/", "/set"
        if topic.startswith(prefix) and topic.endswith(suffix):
            return topic[len(prefix):-len(suffix)]
        return None

    # ---- entity model ----
    def _tube_name(self, mac, tube):
        """Friendly name: device-book alias > advertised name > object_id."""
        for alias, amac in self.core.book.aliases.items():
            if amac == mac:
                return alias
        return tube.get("name") or ha.object_id_tube(mac, self.node_id)

    def _desired(self, snap):
        """The full light-entity set as ``(object_id, target, kind, mac|None, name)``.

        Also refreshes :attr:`meta` (used by :meth:`handle_set`)."""
        ents = [(ha.object_id_all(self.node_id), "all", "all", None, "All Tubes")]
        for gname in self.core.book.groups:
            ents.append((ha.object_id_group(gname, self.node_id), gname, "group", None, gname))
        for mac, tube in snap.items():
            ents.append((ha.object_id_tube(mac, self.node_id), mac, "tube", mac,
                         self._tube_name(mac, tube)))
        for object_id, target, kind, mac, _name in ents:
            self.meta[object_id] = (target, kind, mac)
        return ents

    def _desired_configs(self, snap, prefix):
        """Every retained discovery config we want: ``(key, config_topic, payload)``.

        Covers the light entities, the per-tube diagnostic sensors, and the
        bridge-device telemetry sensors."""
        entries = []
        for object_id, target, kind, mac, name in self._desired(snap):
            if kind == "tube":
                entries.append((object_id, f"{prefix}/light/{object_id}/config",
                                ha.tube_discovery(object_id, name, self.base,
                                                  self.bridge_avail, self.unit)))
                for disc_id, cfg in ha.sensor_discovery(object_id, name, self.base,
                                                        self.bridge_avail):
                    entries.append((disc_id, f"{prefix}/sensor/{disc_id}/config", cfg))
            else:
                entries.append((object_id, f"{prefix}/light/{object_id}/config",
                                ha.group_discovery(object_id, name, self.base,
                                                   self.bridge_avail, self.unit)))
        # bridge-device telemetry sensors (static, always present)
        for disc_id, cfg in ha.bridge_sensor_discovery(self.base, self.bridge_avail):
            entries.append((disc_id, f"{prefix}/sensor/{disc_id}/config", cfg))
        return entries

    # ---- discovery announce / removal ----
    def sync_discovery(self, snap, prefix):
        """Publishes for newly-appeared entities + empty configs for removed ones."""
        pubs = []
        # seed each tube's sliders from what we last sent it (survives a restart)
        for mac, tube in snap.items():
            oid = ha.object_id_tube(mac, self.node_id)
            self.assumed.setdefault(oid, ha.assumed_from_last(tube.get("last", "")))

        desired_keys = set()
        for key, topic, payload in self._desired_configs(snap, prefix):
            desired_keys.add(key)
            if key not in self.announced:
                pubs.append((topic, json.dumps(payload), True))
                self.announced[key] = topic
        # anything previously announced but no longer wanted -> empty retained config removes it
        for key in list(self.announced):
            if key not in desired_keys:
                pubs.append((self.announced.pop(key), "", True))
                for d in (self.meta, self.assumed, self._last_state,
                          self._last_avail, self._last_attrs):
                    d.pop(key, None)
        return pubs

    # ---- availability + state (on change only) ----
    def reconcile(self, snap):
        """Publishes for changed per-tube availability and changed entity state."""
        pubs = []
        for object_id, target, kind, mac, name in self._desired(snap):
            if kind == "tube":
                avail = "online" if snap[mac].get("connected") else "offline"
                if self._last_avail.get(object_id) != avail:
                    pubs.append((self._avail_topic(object_id), avail, True))
                    self._last_avail[object_id] = avail
            snap_tube = snap.get(mac) if kind == "tube" else None
            if kind == "tube":
                attrs = ha.tube_attributes(snap_tube)
                if attrs:
                    ajs = json.dumps(attrs, sort_keys=True)
                    if self._last_attrs.get(object_id) != ajs:
                        pubs.append((f"{self.base}/light/{object_id}/attributes", ajs, True))
                        self._last_attrs[object_id] = ajs
            state = ha.snapshot_to_ha_state(self.assumed.get(object_id, {}), snap_tube, self.unit)
            if not state:
                continue
            js = json.dumps(state, sort_keys=True)
            if self._last_state.get(object_id) != js:
                pubs.append((self._state_topic(object_id), js, True))
                self._last_state[object_id] = js
        return pubs

    def bridge_attributes_pub(self, snap, version, uptime_s):
        """The bridge-telemetry publish (version / uptime / light counts).

        Always emitted (uptime changes every tick) — one small retained topic."""
        attrs = ha.bridge_attributes(snap, version, uptime_s)
        return (f"{self.base}/bridge/attributes", json.dumps(attrs, sort_keys=True), True)

    # ---- incoming set ----
    def handle_set(self, topic, payload_bytes):
        """Translate an HA ``set`` into command lines and update assumed state.

        Returns the command lines to dispatch; the caller then dispatches them and
        publishes :meth:`reconcile`, which emits the (now-changed) entity state.
        Group/``all`` sets also propagate the assumed colour onto member tubes so
        their entities reflect the change.
        """
        object_id = self.object_id_from_set(topic)
        if object_id is None or object_id not in self.meta:
            return []
        try:
            payload = json.loads(payload_bytes)
        except (ValueError, TypeError):
            log.warning("mqtt bad set payload on %s", topic)
            return []
        target, kind, mac = self.meta[object_id]
        lines, new_assumed = ha.ha_set_to_lines(target, payload, self.assumed.get(object_id, {}),
                                                self.unit)
        self.assumed[object_id] = new_assumed
        if kind != "tube":
            # mirror the assumed state onto each affected member tube entity
            for member in self.core.resolve(target):
                member_id = ha.object_id_tube(member, self.node_id)
                if member_id in self.meta:
                    self.assumed[member_id] = dict(new_assumed)
        return lines


async def _publish_all(client, pubs):
    """Await a list of ``(topic, payload, retain)`` publishes."""
    for topic, payload, retain in pubs:
        await client.publish(topic, payload, retain=retain)


async def run(core, cfg) -> None:
    """Connect to the broker (retrying forever); run the HA bridge + legacy topics."""
    host = cfg.get("host", "localhost")
    port = int(cfg.get("port", 1883))
    base = cfg.get("base_topic", "neewer")
    state_interval = float(cfg.get("state_interval", 5.0))
    discovery = bool(cfg.get("discovery", True))
    discovery_prefix = cfg.get("discovery_prefix", "homeassistant")
    node_id = cfg.get("node_id", "neewer")
    color_temp_unit = cfg.get("color_temp_unit", "kelvin")
    legacy = bool(cfg.get("legacy_topics", True))
    # How often to poll the tubes for battery/power/version so the diagnostic
    # sensors stay fresh (0 = only on connect). Slow by default — these change rarely.
    query_interval = float(cfg.get("query_interval", 120.0))

    cmd_topic = f"{base}/cmd"
    state_topic = f"{base}/state"
    start = time.monotonic()
    bridge = _Bridge(core, base, node_id, color_temp_unit)
    # LWT: if the daemon/connection dies, the broker retains "offline" -> HA greys out.
    will = aiomqtt.Will(bridge.bridge_avail, payload="offline", retain=True)

    while True:
        try:
            async with aiomqtt.Client(hostname=host, port=port,
                                      username=cfg.get("username"),
                                      password=cfg.get("password"),
                                      will=will) as client:
                log.info("mqtt connected %s:%s (discovery=%s)", host, port, discovery)
                if discovery:
                    await client.publish(bridge.bridge_avail, "online", retain=True)
                    # populate battery/power/version once up front for the sensors
                    await _dispatch(core, "query all")
                    snap = core.snapshot()
                    await _publish_all(client, bridge.sync_discovery(snap, discovery_prefix))
                    await _publish_all(client, bridge.reconcile(snap))
                    await _publish_all(client, [bridge.bridge_attributes_pub(
                        snap, __version__, time.monotonic() - start)])
                    await client.subscribe(f"{base}/light/+/set")
                if legacy:
                    await client.subscribe(cmd_topic)

                async def reconcile_loop() -> None:
                    """Announce late tubes + publish changed availability/state + bridge
                    telemetry, and the legacy snapshot, every ``state_interval``. Polls
                    the tubes for battery/power every ``query_interval``."""
                    since_query = 0.0
                    while True:
                        await asyncio.sleep(state_interval)
                        since_query += state_interval
                        if discovery and query_interval and since_query >= query_interval:
                            since_query = 0.0
                            await _dispatch(core, "query all")
                        snap = core.snapshot()
                        if discovery:
                            await _publish_all(
                                client, bridge.sync_discovery(snap, discovery_prefix))
                            await _publish_all(client, bridge.reconcile(snap))
                            await _publish_all(client, [bridge.bridge_attributes_pub(
                                snap, __version__, time.monotonic() - start)])
                        if legacy:
                            await client.publish(state_topic, json.dumps(snap), retain=True)

                task = asyncio.create_task(reconcile_loop())
                try:
                    async for msg in client.messages:
                        topic = str(msg.topic)
                        if discovery and bridge.object_id_from_set(topic) is not None:
                            lines = bridge.handle_set(topic, msg.payload)
                            for line in lines:
                                await _dispatch(core, line)
                            await _publish_all(client, bridge.reconcile(core.snapshot()))
                        elif legacy and topic == cmd_topic:
                            await _dispatch(core, msg.payload.decode().strip())
                finally:
                    task.cancel()
                    with contextlib.suppress(BaseException):
                        await task              # let the reconcile loop finish cancelling
                    if discovery:
                        # Best-effort graceful "offline". Shielded + time-bounded so it
                        # still lands even when we're being cancelled at shutdown (a clean
                        # aiomqtt disconnect wouldn't fire the LWT), yet can't block exit.
                        with contextlib.suppress(BaseException):
                            await asyncio.wait_for(
                                asyncio.shield(client.publish(
                                    bridge.bridge_avail, "offline", retain=True)),
                                timeout=1.0)
        except asyncio.CancelledError:
            raise                               # shutdown: exit the reconnect loop cleanly
        except Exception as exc:
            log.warning("mqtt connection lost (%s); retrying in %.0fs", exc, RECONNECT_DELAY)
            await asyncio.sleep(RECONNECT_DELAY)


async def _dispatch(core, line: str) -> None:
    """Run one command line and log the outcome (never raise into the loop)."""
    if not line:
        return
    try:
        reply = await core.dispatch(line)
        log.info("mqtt %r -> %s", line, reply)
    except Exception as exc:
        log.warning("mqtt %r error: %s", line, exc)
