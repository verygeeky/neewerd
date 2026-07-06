"""Tests for :mod:`neewerd.modules.sacn` — packet handling + the send tick.

No sockets or radio: the ``sacn`` package is an optional extra, so we register a
tiny stub before importing the module (mirroring how ``test_mqtt`` stubs
``aiomqtt``) — but ONLY when the real package is absent, so a live run gets the real
library, not the stub. Synthetic ``DataPacket``-shaped objects are fed straight to
the module's packet handler and the send pass (`dmx.send_tick`) runs against a fake
core that records writes.
"""
from __future__ import annotations

import asyncio
import sys
import types

# --- stub `sacn` so `import sacn` in the module resolves ------------------
# Only when the real package is absent (CI): if `sacn` IS installed (dev / a live
# run), leave it alone so the live tests get the real library, not this stub. The
# dry tests below never instantiate the receiver, so the real module is fine too.
try:
    import sacn  # noqa: F401
except ImportError:
    _stub = types.ModuleType("sacn")

    class _sACNreceiver:  # never instantiated in these tests
        def __init__(self, *a, **k):
            pass

    _stub.sACNreceiver = _sACNreceiver
    sys.modules["sacn"] = _stub

from neewer.protocol import dmx, frames  # noqa: E402

from neewerd.modules import sacn  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class FakePacket:
    """A stand-in for ``sacn.DataPacket``: only the attributes the module reads.

    ``dmxData`` is the 512 DMX slots *excluding* the start code (slot 1 == index 0).
    """

    def __init__(self, universe, dmx_data, sequence=0, priority=100, terminated=False):
        self.universe = universe
        self.dmxData = tuple(dmx_data) + (0,) * (dmx.UNIVERSE_SIZE - len(dmx_data))
        self.sequence = sequence
        self.priority = priority
        self.option_StreamTerminated = terminated


def _direct_submit(fn, *args):
    """A `submit` that applies the mutation immediately (no asyncio loop hop)."""
    fn(*args)


def _handle(packet, universes, last_seq, latest, data_loss_hold=True):
    return sacn._handle_packet(
        packet, universes, last_seq, latest, data_loss_hold, _direct_submit)


# --- _sequence_ok ---------------------------------------------------------

def test_sequence_first_packet_accepted():
    last = {}
    assert sacn._sequence_ok(last, 1, 5) is True
    assert last[1] == 5


def test_sequence_forward_accepted():
    last = {1: 5}
    assert sacn._sequence_ok(last, 1, 6) is True


def test_sequence_backward_dropped():
    last = {1: 10}
    assert sacn._sequence_ok(last, 1, 9) is False   # one step back
    assert sacn._sequence_ok(last, 1, 10) is False  # duplicate


def test_sequence_wraparound_accepted():
    last = {1: 255}
    assert sacn._sequence_ok(last, 1, 0) is True     # 255 -> 0 wrap is forward


# --- _handle_packet -------------------------------------------------------

def test_valid_packet_updates_latest():
    latest, last_seq = {}, {}
    tag = _handle(FakePacket(0, [255, 255, 255, 255], sequence=1), {0}, last_seq, latest)
    assert tag == "stored"
    assert latest[0][:4] == [255, 255, 255, 255]
    assert len(latest[0]) == dmx.UNIVERSE_SIZE


def test_unpatched_universe_ignored():
    latest, last_seq = {}, {}
    tag = _handle(FakePacket(9, [1, 2, 3], sequence=1), {0}, last_seq, latest)
    assert tag == "ignored" and latest == {}


def test_out_of_order_sequence_dropped():
    latest, last_seq = {}, {}
    _handle(FakePacket(0, [10, 0, 0, 0], sequence=5), {0}, last_seq, latest)
    # an older sequence for the same universe must not overwrite the latest slots
    tag = _handle(FakePacket(0, [99, 0, 0, 0], sequence=4), {0}, last_seq, latest)
    assert tag == "out_of_order"
    assert latest[0][0] == 10   # unchanged


def test_stream_terminated_hold_keeps_last_slots():
    latest, last_seq = {}, {}
    _handle(FakePacket(0, [77, 0, 0, 0], sequence=1), {0}, last_seq, latest)
    tag = _handle(FakePacket(0, [0, 0, 0, 0], sequence=2, terminated=True),
                  {0}, last_seq, latest, data_loss_hold=True)
    assert tag == "terminated"
    assert latest[0][0] == 77   # held, not cleared


def test_stream_terminated_blackout_zeroes_universe():
    latest, last_seq = {}, {}
    _handle(FakePacket(0, [77, 88, 99, 255], sequence=1), {0}, last_seq, latest)
    _handle(FakePacket(0, [0, 0, 0, 0], sequence=2, terminated=True),
            {0}, last_seq, latest, data_loss_hold=False)
    assert latest[0] == [0] * dmx.UNIVERSE_SIZE


# --- send tick over the stored slots (translate + throttle + write) -------

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


def test_stored_slots_translate_to_frame_on_tick():
    core = FakeCore({"t1": ["AA"]})
    patches = dmx.parse_patch({"t1": {"universe": 1, "address": 1, "personality": "hsi"}})
    latest, last_seq = {}, {}
    # hue 359, sat 100, intensity 100 patched at address 1
    _handle(FakePacket(1, [255, 255, 255, 255], sequence=1), {1}, last_seq, latest)

    written = run(dmx.send_tick(
        core, patches, latest, dmx.RateLimiter(0.04), {"owning": False}, now=1.0))
    assert written == [("AA", frames.hsi(359, 100, 100))]
    assert core.cancels == 1
