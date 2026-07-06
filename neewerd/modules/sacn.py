"""sacn2neewer — drive tubes from an sACN / E1.31 (DMX-over-IP) console.

A lighting desk (grandMA, Eos, Chamsys, Hog, Resolume, ...) patches the tubes and
streams DMX over sACN (E1.31, multicast UDP 5568). Like the ``artnet`` module this
is the **low-rate "throw-and-go" wireless path**: DMX arrives at ~44 Hz, but BLE is
one slow adapter for the whole fleet, so we do NOT write every packet. A receive
callback only remembers the latest slots per universe; a send loop wakes at
``send_hz`` and writes a fixture only when its computed frame actually **changed**
(and no more often than ``min_interval``). The DMX->frame maths, the patch model,
and the throttled send pass (:func:`neewerd.dmx.send_tick`) are shared with the
``artnet`` module in the pure :mod:`neewerd.dmx` core.

Depends on the maintained ``sacn`` package (Hundemeier, MIT) for the ACN framing,
multicast join, and E1.31 receiver — an optional extra (``pip install '.[sacn]'``).
The library already filters by DMX sequence, source priority (highest wins), and
the stream-termination bit before it hands us a packet, and separately reports
network data loss via an ``availability`` callback; we mirror the sequence /
stream-terminated checks locally too so the translation is correct even when a
packet is fed straight to our callback (and so the behaviour is unit-testable
without the socket layer).

Threading note: ``sacn`` runs its receiver on its own thread and fires our callback
there — like ``python-osc`` in ``osc.py``. The callback must NOT touch ``core`` or
the pending-slots dict directly; it hops back onto the asyncio loop with
``loop.call_soon_threadsafe`` to mutate the latest-slots dict, and the async send
loop does all the BLE work. ``receiver.stop()`` is called in a ``finally``.

Architecture note: like every module this keeps ``core`` the sole owner of
Bluetooth, but for frame-rate traffic it uses core's low-level ``resolve`` /
``write`` directly (and cancels a running effect **once** on first DMX) rather than
``core.dispatch`` per packet. No new ``core`` methods are used.
"""
from __future__ import annotations

import asyncio
import logging

import sacn
from neewer.protocol import dmx

log = logging.getLogger("neewerd.sacn")

#: sACN / E1.31 rides on UDP 5568; the library binds this by default. Informational
#: default for the ``port`` config knob.
SACN_PORT = 5568

#: E1.31 §6.7.2: a packet is "out of order" when its 8-bit sequence number is a small
#: step (1..19) *behind* the last seen one (or a duplicate, diff 0). A big backwards
#: jump is a wrap-around and counts as forward. The library enforces this too; we
#: repeat it so a packet handed straight to the callback is still checked.
_SEQUENCE_REORDER_WINDOW = 20


def _sequence_ok(last_seq: dict, universe: int, sequence: int) -> bool:
    """Return True (and record) if ``sequence`` is not an out-of-order/duplicate for
    ``universe``; False (dropping it) if it is. Mirrors the E1.31 reject rule."""
    prev = last_seq.get(universe)
    if prev is not None:
        diff = sequence - prev
        # diff in ]-20, 0]: an old or duplicate packet -> drop. (A big negative diff
        # is a legitimate 255->0 wrap and passes.)
        if -_SEQUENCE_REORDER_WINDOW < diff <= 0:
            return False
    last_seq[universe] = sequence
    return True


def _apply_loss(latest: dict, universe: int, hold: bool) -> None:
    """Handle data loss (stream terminated / network timeout) for ``universe``.

    ``hold`` True keeps the last slots so the send loop simply repeats the frozen
    look; False blacks the universe out (all slots 0 -> intensity 0). Runs on the
    asyncio loop (scheduled via ``call_soon_threadsafe``)."""
    if hold:
        return
    latest[universe] = [0] * dmx.UNIVERSE_SIZE


def _handle_packet(packet, universes, last_seq, latest, data_loss_hold, submit) -> str:
    """Process one received sACN ``DataPacket``; return a short tag for logging/tests.

    Runs on the ``sacn`` receiver thread. It reads packet attributes and mutates the
    thread-local ``last_seq`` here, but every mutation of the shared ``latest`` dict
    is deferred to the asyncio loop through ``submit`` (in :func:`run` that is
    ``loop.call_soon_threadsafe``; tests pass a direct caller). ``packet.dmxData`` is
    the 512 DMX slots *excluding* the start code, so slot 1 == ``dmxData[0]`` — the
    same 1-based convention the patch model uses.
    """
    universe = packet.universe
    if universe not in universes:      # ignore universes we aren't patched to
        return "ignored"
    if packet.option_StreamTerminated:  # source cleanly ended the stream -> data loss
        last_seq.pop(universe, None)    # reset ordering so the next stream starts fresh
        submit(_apply_loss, latest, universe, data_loss_hold)
        return "terminated"
    if not _sequence_ok(last_seq, universe, packet.sequence):
        return "out_of_order"
    submit(latest.__setitem__, universe, list(packet.dmxData))
    return "stored"


async def run(core, cfg) -> None:
    """Receive sACN and drive the patched fixtures at a throttled rate."""
    host = cfg.get("host", "0.0.0.0")
    port = int(cfg.get("port", SACN_PORT))
    send_hz = float(cfg.get("send_hz", 30.0))
    data_loss_hold = bool(cfg.get("data_loss_hold", True))
    patches = dmx.parse_patch(cfg.get("patch", {}))
    universes = {p.universe for p in patches}
    limiter = dmx.RateLimiter(float(cfg.get("min_interval", 0.04)))
    #: Per-tube BBR-style pacers (#46) — same knobs/auto-tuning as the artnet
    #: module; skip-a-tick (drop-newest) instead of piling BlueZ's TX queue.
    governors = dmx.governors_from_cfg(cfg)

    #: latest slots per subscribed universe; the receive callback only overwrites.
    latest: dict[int, list[int]] = {}
    #: last DMX sequence seen per universe (touched only on the receiver thread).
    last_seq: dict[int, int] = {}
    state = {"owning": False}

    loop = asyncio.get_running_loop()

    def on_packet(packet) -> None:
        # Called on the sacn receiver thread; hop the dict mutation onto the loop.
        _handle_packet(packet, universes, last_seq, latest, data_loss_hold,
                       loop.call_soon_threadsafe)

    def on_availability(universe, changed) -> None:
        # 'timeout' = no packets for E131_NETWORK_DATA_LOSS_TIMEOUT_ms (network drop);
        # 'available' = first packet arrived (nothing to do). Same data-loss policy.
        if changed == "timeout" and universe in universes:
            loop.call_soon_threadsafe(_apply_loss, latest, universe, data_loss_hold)

    receiver = sacn.sACNreceiver(bind_address=host, bind_port=port)
    # 'universe' fires per-universe DMX; 'availability' fires network up/down per universe.
    for universe in universes:
        receiver.register_listener("universe", on_packet, universe=universe)
    receiver.register_listener("availability", on_availability)
    receiver.start()
    for universe in universes:
        receiver.join_multicast(universe)   # 239.255.<hi>.<lo> group for each universe
    log.info("sacn listening on %s:%s (%d patch(es), %d universe(s))",
             host, port, len(patches), len(universes))
    try:
        while True:
            await asyncio.sleep(1.0 / send_hz)
            try:
                await dmx.send_tick(core, patches, latest, limiter, state,
                                    loop.time(), governors)
            except Exception as exc:
                log.warning("sacn tick error: %s", exc)
    finally:
        receiver.stop()
