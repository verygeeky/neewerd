"""Tests for :mod:`neewerd.modules.artnet` — ArtDmx parsing + the send tick.

No sockets or radio: ArtDmx packets are built by hand with ``struct`` and the send
pass (`dmx.send_tick`) runs against a fake core that records writes.
"""
from __future__ import annotations

import asyncio
import struct

from neewer.protocol import dmx, frames

from neewerd.modules import artnet


def run(coro):
    return asyncio.run(coro)


def make_artdmx(universe: int, data: list[int], seq: int = 0) -> bytes:
    """Build a minimal valid ArtDmx packet for ``universe`` carrying ``data``."""
    sub_uni = universe & 0xFF
    net = (universe >> 8) & 0xFF
    return (
        artnet.ARTNET_ID
        + struct.pack("<H", artnet.OP_DMX)      # opcode (LE)
        + struct.pack(">H", 14)                 # protocol version (BE)
        + bytes([seq, 0, sub_uni, net])         # seq, physical, sub_uni, net
        + struct.pack(">H", len(data))          # slot count (BE)
        + bytes(data)
    )


# --- parse_artdmx ---------------------------------------------------------

def test_parse_artdmx_roundtrip():
    pkt = make_artdmx(5, [10, 20, 30])
    assert artnet.parse_artdmx(pkt) == (5, [10, 20, 30])


def test_parse_artdmx_universe_high_byte():
    # net in the high byte: universe 0x0102
    assert artnet.parse_artdmx(make_artdmx(0x0102, [1]))[0] == 0x0102


def test_parse_artdmx_rejects_non_artnet():
    assert artnet.parse_artdmx(b"not art-net at all............") is None


def test_parse_artdmx_rejects_wrong_opcode():
    # ArtPoll opcode 0x2000, not ArtDmx
    pkt = artnet.ARTNET_ID + struct.pack("<H", 0x2000) + b"\x00" * 8
    assert artnet.parse_artdmx(pkt) is None


def test_parse_artdmx_rejects_short():
    assert artnet.parse_artdmx(b"Art-Net\x00") is None


# --- dmx.send_tick (translate + throttle + write) ----------------------------

class FakeCore:
    """Records writes and effect-cancels; resolves targets from a fixed map."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.writes: list[tuple[str, bytes]] = []
        self.cancels = 0

    def resolve(self, target):
        return list(self.mapping.get(target, []))

    async def write(self, mac, frame):
        self.writes.append((mac, frame))
        return True

    async def cancel_effect(self):
        self.cancels += 1


def _hsi_universe(hue_msb, hue_lsb, sat, intensity):
    """A 512-slot universe with an hsi patch's 4 channels at address 1."""
    data = [hue_msb, hue_lsb, sat, intensity] + [0] * 508
    return data


def test_emit_tick_writes_translated_frame_and_cancels_once():
    core = FakeCore({"t1": ["AA"]})
    patches = dmx.parse_patch({"t1": {"universe": 0, "address": 1, "personality": "hsi"}})
    latest = {0: _hsi_universe(255, 255, 255, 255)}   # hue 359, sat 100, int 100
    limiter = dmx.RateLimiter(0.04)
    state = {"owning": False}

    written = run(dmx.send_tick(core, patches, latest, limiter, state, now=1.0))
    assert written == [("AA", frames.hsi(359, 100, 100))]
    assert core.cancels == 1 and state["owning"] is True

    # a second identical tick writes nothing (frame unchanged) and doesn't re-cancel
    written2 = run(dmx.send_tick(core, patches, latest, limiter, state, now=2.0))
    assert written2 == []
    assert core.cancels == 1


def test_emit_tick_change_after_interval_writes_again():
    core = FakeCore({"t1": ["AA"]})
    patches = dmx.parse_patch({"t1": {"universe": 0, "address": 1, "personality": "hsi"}})
    limiter = dmx.RateLimiter(0.04)
    state = {"owning": False}

    latest = {0: _hsi_universe(0, 0, 255, 255)}
    run(dmx.send_tick(core, patches, latest, limiter, state, now=1.0))
    # change the colour; enough time passes -> a second write
    latest[0] = _hsi_universe(255, 255, 255, 255)
    run(dmx.send_tick(core, patches, latest, limiter, state, now=1.1))
    assert core.writes == [
        ("AA", frames.hsi(0, 100, 100)),
        ("AA", frames.hsi(359, 100, 100)),
    ]


def test_emit_tick_skips_patches_without_data():
    core = FakeCore({"t1": ["AA"]})
    patches = dmx.parse_patch({"t1": {"universe": 7, "address": 1, "personality": "hsi"}})
    # no data for universe 7 yet
    written = run(dmx.send_tick(
        core, patches, {}, dmx.RateLimiter(), {"owning": False}, now=1.0))
    assert written == [] and core.writes == [] and core.cancels == 0


def test_emit_tick_group_target_writes_each_member():
    core = FakeCore({"keys": ["AA", "BB"]})
    patches = dmx.parse_patch({"keys": {"universe": 0, "address": 1, "personality": "hsi"}})
    latest = {0: _hsi_universe(255, 255, 255, 255)}
    written = run(dmx.send_tick(
        core, patches, latest, dmx.RateLimiter(), {"owning": False}, now=1.0))
    assert [mac for mac, _ in written] == ["AA", "BB"]


# --- write-governor wiring + perf telemetry (#46) ---------------------------

def test_send_tick_with_module_governors_paces_writes():
    # The module hands send_tick a GovernorBook built from its config table:
    # an over-demanded tube is skipped for the tick (drop-newest), never queued.
    core = FakeCore({"t1": ["AA"]})
    patches = dmx.parse_patch({"t1": {"universe": 0, "address": 1, "personality": "hsi"}})
    limiter = dmx.RateLimiter(0.0)
    governors = dmx.governors_from_cfg({"rate_min": 1.0, "rate_max": 1.0})  # 1 write/s
    state = {"owning": False}

    run(dmx.send_tick(core, patches, {0: _hsi_universe(0, 0, 255, 255)},
                      limiter, state, 1.0, governors))
    run(dmx.send_tick(core, patches, {0: _hsi_universe(255, 255, 255, 255)},
                      limiter, state, 1.05, governors))       # changed, but paced out
    assert len(core.writes) == 1
    assert governors["AA"].deferred == 1


def test_tube_stats_formats_governor_state():
    gov = dmx.WriteGovernor(rate_init=20.0)
    gov.on_delivery(0.0, rtt=0.012)
    frag = artnet._tube_stats("AA:BB:CC:DD:EE:01", writes=100, gov=gov,
                              deferred=5, elapsed=5.0)
    assert frag == "AA:BB:CC=20/s rate=20 bw=0 rtt=12ms def=1.0/s"


def test_tube_stats_without_governor_is_rate_only():
    assert artnet._tube_stats("AA", 50, None, 0, 5.0) == "AA=10/s"
