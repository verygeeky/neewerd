"""artnet2neewer — drive tubes from an Art-Net (DMX-over-IP) console.

A lighting desk (QLab, grandMA, Eos, Chamsys, Resolume, ...) patches the tubes and
drives them from faders/cues over Art-Net (UDP 6454). This is the **low-rate
"throw-and-go" wireless path**: DMX arrives at ~44 Hz, but BLE is a single slow
adapter for the whole fleet, so we do NOT write every packet. A receive callback
just remembers the latest slots per universe; a send loop wakes at ``send_hz`` and
writes a fixture only when its computed frame actually **changed** (and no more
often than ``min_interval``).

Only stdlib is needed — Art-Net's ArtDmx is a few bytes of ``struct`` (no external
dependency). The DMX->frame maths + patch model live in the pure :mod:`neewerd.dmx`.

Architecture note: like every module this keeps ``core`` the sole owner of
Bluetooth, but for frame-rate traffic it uses core's low-level ``resolve`` /
``write`` directly (and cancels a running effect **once** on first DMX) rather than
``core.dispatch`` per packet — dispatch re-parses a string and cancels effects on
every call, which is wrong 25×/second. No new ``core`` methods are used.

Write control (#46): each patched tube gets a :class:`neewer.protocol.dmx.WriteGovernor`
— a BBR-style pacer that keeps the issue rate at-or-below the tube's *measured*
delivery rate (``write-without-response`` has no backpressure; overfeeding piles
frames unbounded in BlueZ's per-connection TX queue). A tube past its pace is
skipped for the tick (drop-newest), never queued. Fully auto-tuning; the optional
``[modules.artnet]`` knobs (``rate_min``/``rate_max``/``probe_interval``/
``probe_rtt_interval``/``increase_factor``/``rtt_congestion_k``) only pin bounds.
A periodic per-tube canary (``core.canary``, a query->notify round trip) feeds
RTT samples to the governors; ``canary_interval = 0`` disables it (the governor
still self-tunes on the issue-rate estimate alone).
"""
from __future__ import annotations

import asyncio
import logging
import struct

from neewer.protocol import dmx

log = logging.getLogger("neewerd.artnet")

#: Art-Net lives on UDP 6454 and every packet starts with this ID.
ARTNET_PORT = 6454
ARTNET_ID = b"Art-Net\x00"
OP_DMX = 0x5000            # ArtDmx (opcode is little-endian on the wire)

#: An ArtDmx header is 18 bytes; slot data follows.
_ARTDMX_HEADER = 18


def parse_artdmx(packet: bytes):
    """Parse an ArtDmx packet into ``(universe, [slots])`` or ``None``.

    Ignores anything that isn't a well-formed ArtDmx (wrong ID / opcode / too
    short). Universe = ``net<<8 | sub_uni``; slot 1 is ``data[0]``.
    """
    if len(packet) < _ARTDMX_HEADER or not packet.startswith(ARTNET_ID):
        return None
    opcode = struct.unpack_from("<H", packet, 8)[0]     # opcode is little-endian
    if opcode != OP_DMX:
        return None
    sub_uni = packet[14]
    net = packet[15]
    length = struct.unpack_from(">H", packet, 16)[0]    # slot count is big-endian
    universe = (net << 8) | sub_uni
    slots = list(packet[_ARTDMX_HEADER:_ARTDMX_HEADER + length])
    return universe, slots


class _ArtNetProtocol(asyncio.DatagramProtocol):
    """Feeds each parsed ArtDmx into ``on_dmx(universe, slots)`` on the loop."""

    def __init__(self, on_dmx):
        self.on_dmx = on_dmx

    def datagram_received(self, data, addr):
        parsed = parse_artdmx(data)
        if parsed is not None:
            self.on_dmx(*parsed)


async def run(core, cfg) -> None:
    """Receive Art-Net and drive the patched fixtures at a throttled rate."""
    host = cfg.get("host", "0.0.0.0")
    port = int(cfg.get("port", ARTNET_PORT))
    send_hz = float(cfg.get("send_hz", 30.0))
    patches = dmx.parse_patch(cfg.get("patch", {}))
    universes = {p.universe for p in patches}
    limiter = dmx.RateLimiter(float(cfg.get("min_interval", 0.04)))
    #: Per-tube BBR-style pacers (#46); auto-created per MAC, zero-config default.
    governors = dmx.governors_from_cfg(cfg)
    #: Publish the live governor dict onto core so read-only consumers (the http
    #: module's /api/v1/state + SSE console telemetry) can surface per-tube write
    #: pacing without owning the artnet loop. The dict is live — reads see fresh
    #: counters — so no snapshot/copy is needed.
    core.write_governors = governors

    #: latest slots per subscribed universe; the receive callback only overwrites.
    latest: dict[int, list[int]] = {}
    state = {"owning": False}

    def on_dmx(universe, slots):
        if universe in universes:      # ignore universes we aren't patched to
            latest[universe] = slots

    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _ArtNetProtocol(on_dmx), local_addr=(host, port))
    log.info("artnet listening on %s:%s (%d patch(es), %d universe(s))",
             host, port, len(patches), len(universes))

    # Latency canary (#46): a periodic per-tube query->notify round trip whose RTT
    # feeds the governors — a rising canary RTT is the queue-building congestion
    # signal the write path itself can't see. Optional: needs core.canary (the
    # library fleet has it; a minimal core doesn't) and canary_interval > 0.
    canary_interval = float(cfg.get("canary_interval", 2.0))

    async def canary_loop() -> None:
        while True:
            await asyncio.sleep(canary_interval)
            for mac in list(governors):         # only tubes the send pass has driven
                try:
                    rtt = await core.canary(mac)
                except Exception as exc:
                    log.debug("canary %s failed: %s", mac, exc)
                    continue
                if rtt is not None:
                    # The canary reply is itself a delivered round trip: one
                    # delivery sample + the RTT that min-filters/congestion-checks.
                    governors[mac].on_delivery(loop.time(), rtt)

    canary_task = None
    if canary_interval > 0 and hasattr(core, "canary"):
        canary_task = asyncio.create_task(canary_loop())

    # Lightweight throughput telemetry: send_tick returns the (mac, frame) writes it
    # actually performed after change-detection + governor pacing, so we can report
    # the real per-tube BLE write rate — the true light-side ceiling, distinct from
    # the incoming DMX/Art-Net rate — plus each governor's control state (rate/bw/
    # min_rtt/deferred-per-s: a sustained def>0 IS the "we would have backlogged"
    # signal). Logged once per window, and ONLY when there was traffic (writes or
    # deferrals), so an idle patch stays silent.
    STATS_WINDOW = float(cfg.get("stats_window", 5.0))
    writes: dict[str, int] = {}
    deferred_seen: dict[str, int] = {}      # governor.deferred at last window edge
    window_start = loop.time()
    try:
        while True:
            await asyncio.sleep(1.0 / send_hz)
            now = loop.time()
            try:
                written = await dmx.send_tick(
                    core, patches, latest, limiter, state, now, governors)
                for mac, _frame in written or ():
                    writes[mac] = writes.get(mac, 0) + 1
            except Exception as exc:
                log.warning("artnet tick error: %s", exc)
            elapsed = now - window_start
            if elapsed >= STATS_WINDOW:
                # Per-tube deferrals this window (counter deltas), including tubes
                # that were starved so hard they never got a write in.
                deferrals = {}
                for mac, gov in governors.items():
                    delta = gov.deferred - deferred_seen.get(mac, 0)
                    deferred_seen[mac] = gov.deferred
                    if delta:
                        deferrals[mac] = delta
                if writes or deferrals:
                    per = ", ".join(
                        _tube_stats(m, writes.get(m, 0), governors.get(m),
                                    deferrals.get(m, 0), elapsed)
                        for m in sorted(set(writes) | set(deferrals)))
                    log.info("artnet perf: %.0f writes/s over %d tube(s) [%s]",
                             sum(writes.values()) / elapsed, len(writes), per)
                writes.clear()
                window_start = now
    finally:
        if canary_task is not None:
            canary_task.cancel()
        transport.close()


def _tube_stats(mac: str, writes: int, gov, deferred: int, elapsed: float) -> str:
    """One tube's perf-line fragment: write rate + governor control state (#46)."""
    part = f"{mac[:8]}={writes / elapsed:.0f}/s"
    if gov is None:                 # no governor yet (tube never gated) — rate only
        return part
    rtt_ms = f"{gov.min_rtt * 1000:.0f}" if gov.min_rtt is not None else "-"
    return (f"{part} rate={gov.rate:.0f} bw={gov.bw:.0f} "
            f"rtt={rtt_ms}ms def={deferred / elapsed:.1f}/s")
