"""artnet_bridge — Art-Net **in** → Art-Net **out**, wrapping the TL120C pixel personality.

The odd module out: every other DMX front-end here (``modules/artnet``, ``modules/sacn``)
receives DMX-over-IP and drives tubes over **BLE**. This one never touches Bluetooth. It
receives a *simple* pixel stream — any Art-Net source speaking plain RGB / RGBW / RGBCW,
N channels per pixel — and re-emits it as Art-Net in the TL120C's **32-pixel custom**
personality, unicast to an Art-Net (DMX-over-IP) node that outputs wired DMX512 into each
tube's RJ45 port.

Why it exists: the tube's per-pixel personality is deliberately awkward — a mode byte, a pixel-
count byte, then 32 × 7 channels ``[colour-mode, brightness, R, G, B, cold-white, warm-white]``
(see ``neewer-hardware/dmx.md``). Most pixel software only emits flat RGB/RGBW and can't write
that layout. This module owns the awkward framing so the source stays simple: point it at us as
an ordinary strip, and we do the wrapping and the CW/WW handling on the way out.

  pixel source ──ArtDmx(RGB/RGBW)──▶ artnet_bridge ──ArtDmx(32-px custom)──▶ node ──DMX──▶ tubes

Each tube is one entry in ``[modules.artnet_bridge.tube.<name>]`` mapping an input slice
``(in_universe, in_address)`` to an output slice ``(out_universe, out_address)``. The input
stride is set by ``personality`` — rgb=3 / rgbw=4 / rgbcw=5 / rgbaw=5 / rgbwa=5 channels per
pixel — module-wide by default and overridable per tube, so one rig can mix front-end formats.
The output footprint is always ``2 + pixels × 7`` channels (226 for the full 32-pixel tube), so
two tubes fit one output universe at addresses 1 and 227 — the standard packing. Front-end
packing is free-form: give each tube its own input universe, or bin-pack several into one via
``in_address`` (the loader rejects any tube whose input or output slice runs past channel 512).

Output is a steady refresh: hardware DMX nodes expect continuous frames (many blackout on
timeout), so once any input has arrived we send every output universe every ``fps`` tick
regardless of change. Unicast to ``dest`` means our own output never loops back into the
listener, even if an in- and out-universe number happen to collide.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import struct
from dataclasses import dataclass

from .artnet import ARTNET_ID, ARTNET_PORT, OP_DMX, parse_artdmx

log = logging.getLogger("neewerd.artnet_bridge")

# --- TL120C 32-pixel custom personality (neewer-hardware/dmx.md) -------------
#: Start channel `n`: value in 160-191 selects 32-pixel custom mode.
PIXEL_MODE_VAL = 175
#: `n+1`: pixel count; 204-254 selects the full 32 pixels.
PIXEL_COUNT_VAL = 230
#: Per-pixel colour-mode byte: 78-115 selects RGBCW (colour channels applied directly).
PER_PIXEL_RGBCW = 96
#: Channels per pixel in RGBCW: colour-mode, brightness, R, G, B, cold-white, warm-white.
CH_PER_PIXEL = 7
#: Two header channels (mode, count) precede the per-pixel data.
HEADER_CH = 2

UNIVERSE_SIZE = 512

#: Input personality -> channels per pixel on the incoming stream.
IN_STRIDE = {"rgb": 3, "rgbw": 4, "rgbcw": 5, "rgbaw": 5, "rgbwa": 5}


def decode_pixel(personality: str, px: list[int]) -> tuple[int, int, int, int, int]:
    """One input pixel's channels -> ``(r, g, b, cold_white, warm_white)`` (each 0-255).

    * ``rgb``   — ``[R,G,B]``; no dedicated white (CW=WW=0).
    * ``rgbw``  — ``[R,G,B,W]``; the single white feeds BOTH emitters (CW=WW=W), a neutral
      dedicated white — the sensible default when the source can't split colour temperature
      (mirrors the ``rgbw`` BLE personality in ``neewer.protocol.dmx``).
    * ``rgbcw`` — ``[R,G,B,CW,WW]``; passed straight through for full cold/warm control.
    * ``rgbaw`` — ``[R,G,B,A,W]`` / ``rgbwa`` — ``[R,G,B,W,A]``: a source with amber + white
      emitters but no cold/warm split (a common console fixture — e.g. Lightjams' RGBAW).
      Amber is the warm primary, so **white -> cold-white, amber -> warm-white** — an
      approximate colour-temperature control (amber isn't a true 2700 K warm white, but tracks
      warmth). The two names differ only in the source's channel order.

    A short slice (a packet that didn't carry all our channels) is zero-padded by the caller.
    """
    r, g, b = px[0], px[1], px[2]
    if personality == "rgb":
        return r, g, b, 0, 0
    if personality == "rgbw":
        w = px[3]
        return r, g, b, w, w
    if personality == "rgbaw":          # [R,G,B,A,W]: white -> cold, amber -> warm
        return r, g, b, px[4], px[3]
    if personality == "rgbwa":          # [R,G,B,W,A]: white -> cold, amber -> warm
        return r, g, b, px[3], px[4]
    return r, g, b, px[3], px[4]        # rgbcw: [R,G,B,CW,WW] straight through


def wrap_tube(dmx: bytearray, out_address: int, pixels: list[tuple[int, int, int, int, int]]) -> None:
    """Write one TL120C 32-pixel-custom block into ``dmx`` at 1-based ``out_address``.

    ``pixels`` is a list of ``(r,g,b,cw,ww)`` tuples. Per-pixel brightness is the master
    level: 0 forces the pixel dark, else full (255) so the colour channels carry the level —
    a dim input colour stays dim without a second scaling. Mutates ``dmx`` in place.
    """
    b = out_address - 1                                 # 1-based channel -> 0-based index
    dmx[b + 0] = PIXEL_MODE_VAL
    dmx[b + 1] = PIXEL_COUNT_VAL
    for k, (r, g, bl, cw, ww) in enumerate(pixels):
        o = b + HEADER_CH + k * CH_PER_PIXEL
        dmx[o + 0] = PER_PIXEL_RGBCW
        dmx[o + 1] = 255 if (r or g or bl or cw or ww) else 0
        dmx[o + 2] = r & 0xFF
        dmx[o + 3] = g & 0xFF
        dmx[o + 4] = bl & 0xFF
        dmx[o + 5] = cw & 0xFF
        dmx[o + 6] = ww & 0xFF


def build_artdmx(universe: int, data: bytes, seq: int) -> bytes:
    """Assemble an ArtDmx packet (inverse of :func:`neewerd.modules.artnet.parse_artdmx`)."""
    return (
        ARTNET_ID
        + struct.pack("<H", OP_DMX)                     # opcode (LE)
        + struct.pack(">H", 14)                         # protocol version (BE)
        + bytes([seq & 0xFF, 0, universe & 0xFF, (universe >> 8) & 0xFF])  # seq, phys, sub, net
        + struct.pack(">H", len(data))                  # slot count (BE)
        + bytes(data)
    )


@dataclass(frozen=True)
class TubeMap:
    """One tube: an input pixel slice re-emitted as a TL120C block at an output slice.

    ``personality`` and ``pixels`` are per-tube — each falls back to the module default
    (``[modules.artnet_bridge] personality/pixels``) but a tube may override either, so one
    rig can mix front-end formats (e.g. an rgbw tube packed beside an rgbaw tube).
    """

    name: str
    in_universe: int
    in_address: int          # 1-based start channel of pixel 0 on the input stream
    out_universe: int
    out_address: int         # 1-based start channel of the TL120C block on the output
    personality: str         # rgb / rgbw / rgbcw / rgbaw / rgbwa — this tube's input format
    pixels: int              # pixel count for this tube

    def out_footprint(self) -> int:
        """Channels this tube occupies on its output universe (the TL120C custom block)."""
        return HEADER_CH + self.pixels * CH_PER_PIXEL

    def in_footprint(self) -> int:
        """Channels this tube consumes on its input universe (pixels × personality stride)."""
        return self.pixels * IN_STRIDE[self.personality]

    def read_pixels(self, universe_data: list[int]) -> list[tuple[int, int, int, int, int]]:
        """Slice this tube's pixels out of an input universe, decoded to (r,g,b,cw,ww)."""
        stride = IN_STRIDE[self.personality]
        start = self.in_address - 1
        out = []
        for k in range(self.pixels):
            o = start + k * stride
            px = universe_data[o:o + stride]
            if len(px) < stride:
                px = list(px) + [0] * (stride - len(px))
            out.append(decode_pixel(self.personality, px))
        return out


def parse_tubes(cfg: dict, default_personality: str, default_pixels: int) -> list[TubeMap]:
    """Parse ``[modules.artnet_bridge.tube.*]`` into validated :class:`TubeMap` objects.

    ``default_personality``/``default_pixels`` are the module-level values a tube inherits
    unless it sets its own ``personality``/``pixels``. Raises ``ValueError`` on a missing key,
    an unknown personality, or an input **or** output block that would run past channel 512 —
    a mis-patched fixture should fail loudly at start, not silently corrupt a frame.
    """
    tubes: list[TubeMap] = []
    for name, spec in cfg.get("tube", {}).items():
        personality = str(spec.get("personality", default_personality)).lower()
        if personality not in IN_STRIDE:
            raise ValueError(
                f"tube {name!r}: unknown personality {personality!r} "
                f"(known: {', '.join(sorted(IN_STRIDE))})")
        try:
            t = TubeMap(
                name=name,
                in_universe=int(spec["in_universe"]),
                in_address=int(spec["in_address"]),
                out_universe=int(spec["out_universe"]),
                out_address=int(spec["out_address"]),
                personality=personality,
                pixels=int(spec.get("pixels", default_pixels)),
            )
        except KeyError as exc:
            raise ValueError(f"tube {name!r}: missing key {exc}") from None
        out_end = t.out_address + t.out_footprint() - 1
        if t.out_address < 1 or out_end > UNIVERSE_SIZE:
            raise ValueError(
                f"tube {name!r}: out_address {t.out_address} + {t.out_footprint()} "
                f"channels runs past DMX channel {UNIVERSE_SIZE}")
        in_end = t.in_address + t.in_footprint() - 1
        if t.in_address < 1 or in_end > UNIVERSE_SIZE:
            raise ValueError(
                f"tube {name!r}: in_address {t.in_address} + {t.in_footprint()} "
                f"channels runs past DMX channel {UNIVERSE_SIZE}")
        tubes.append(t)
    return tubes


def render_universe(out_universe: int, tubes: list[TubeMap],
                    latest: dict[int, list[int]]) -> bytearray:
    """Build the full 512-slot output buffer for one output universe from the latest input.

    Each tube carries its own ``personality``/``pixels``, so one output universe can pack
    tubes of different front-end formats.
    """
    dmx = bytearray(UNIVERSE_SIZE)
    for t in tubes:
        if t.out_universe != out_universe:
            continue
        src = latest.get(t.in_universe)
        px = (t.read_pixels(src) if src is not None
              else [(0, 0, 0, 0, 0)] * t.pixels)
        wrap_tube(dmx, t.out_address, px)
    return dmx


class _ArtNetProtocol(asyncio.DatagramProtocol):
    """Stash the latest slots for each input universe we care about."""

    def __init__(self, wanted: set[int], latest: dict[int, list[int]]):
        self.wanted = wanted
        self.latest = latest

    def datagram_received(self, data, addr):
        parsed = parse_artdmx(data)
        if parsed is not None and parsed[0] in self.wanted:
            self.latest[parsed[0]] = parsed[1]


async def run(core, cfg) -> None:
    """Receive a simple pixel stream and re-emit it as TL120C 32-pixel-custom Art-Net.

    ``core`` is unused — this bridge is pure network I/O and never touches BLE.
    """
    listen_host = cfg.get("listen_host", "0.0.0.0")
    listen_port = int(cfg.get("listen_port", ARTNET_PORT))
    dest = cfg["dest"]                                  # required: the hardware node's IP
    dest_port = int(cfg.get("dest_port", ARTNET_PORT))
    fps = float(cfg.get("fps", 44.0))
    default_personality = str(cfg.get("personality", "rgb")).lower()
    if default_personality not in IN_STRIDE:
        raise ValueError(f"unknown personality {default_personality!r} "
                         f"(known: {', '.join(sorted(IN_STRIDE))})")
    default_pixels = int(cfg.get("pixels", 32))
    tubes = parse_tubes(cfg, default_personality, default_pixels)
    if not tubes:
        log.warning("artnet_bridge: no [modules.artnet_bridge.tube.*] mapped — nothing to do")
        return

    in_universes = {t.in_universe for t in tubes}
    out_universes = sorted({t.out_universe for t in tubes})
    latest: dict[int, list[int]] = {}

    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _ArtNetProtocol(in_universes, latest), local_addr=(listen_host, listen_port))
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    personalities = ",".join(sorted({t.personality for t in tubes}))
    log.info("artnet_bridge: %d tube(s), in-uni %s @ %s:%d -> out-uni %s @ %s:%d, "
             "personality=%s %.0ffps",
             len(tubes), sorted(in_universes), listen_host, listen_port,
             out_universes, dest, dest_port, personalities, fps)

    seq = 1
    try:
        while True:
            await asyncio.sleep(1.0 / fps)
            if not latest:                              # nothing received yet — stay quiet
                continue
            for uni in out_universes:
                dmx = render_universe(uni, tubes, latest)
                tx.sendto(build_artdmx(uni, bytes(dmx), seq), (dest, dest_port))
            seq = seq % 255 + 1
    finally:
        transport.close()
        tx.close()
