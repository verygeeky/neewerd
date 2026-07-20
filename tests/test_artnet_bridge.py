"""Tests for :mod:`neewerd.modules.artnet_bridge` — the Art-Net->Art-Net pixel wrapper.

Pure functions only (no sockets): decode a pixel, wrap a tube, build/parse an ArtDmx,
validate the patch, and render a whole output universe from a synthetic input universe.
"""
from __future__ import annotations

from neewerd.modules import artnet, artnet_bridge as ab

import pytest


# --- decode_pixel ---------------------------------------------------------

def test_decode_rgb_has_no_white():
    assert ab.decode_pixel("rgb", [10, 20, 30]) == (10, 20, 30, 0, 0)


def test_decode_rgbw_feeds_both_whites():
    assert ab.decode_pixel("rgbw", [10, 20, 30, 200]) == (10, 20, 30, 200, 200)


def test_decode_rgbcw_passthrough():
    assert ab.decode_pixel("rgbcw", [10, 20, 30, 40, 50]) == (10, 20, 30, 40, 50)


def test_decode_rgbaw_white_cold_amber_warm():
    # source order [R,G,B,A,W]; A=40 -> warm-white, W=50 -> cold-white
    assert ab.decode_pixel("rgbaw", [10, 20, 30, 40, 50]) == (10, 20, 30, 50, 40)


def test_decode_rgbwa_white_cold_amber_warm():
    # source order [R,G,B,W,A]; W=40 -> cold-white, A=50 -> warm-white
    assert ab.decode_pixel("rgbwa", [10, 20, 30, 40, 50]) == (10, 20, 30, 40, 50)


# --- wrap_tube ------------------------------------------------------------

def test_wrap_tube_header_and_first_pixel():
    dmx = bytearray(512)
    px = [(255, 0, 0, 0, 0)] + [(0, 0, 0, 0, 0)] * 31
    ab.wrap_tube(dmx, 227, px)
    assert dmx[226] == ab.PIXEL_MODE_VAL       # channel 227: mode
    assert dmx[227] == ab.PIXEL_COUNT_VAL      # channel 228: count
    # channel 229..235: pixel 0 = colour-mode, bri, R, G, B, CW, WW
    assert list(dmx[228:235]) == [ab.PER_PIXEL_RGBCW, 255, 255, 0, 0, 0, 0]


def test_wrap_tube_black_pixel_brightness_zero():
    dmx = bytearray(512)
    ab.wrap_tube(dmx, 1, [(0, 0, 0, 0, 0)] * 32)
    # pixel 0 brightness (channel 4 of the block, index base+1+HEADER... = index 3) is 0
    assert dmx[3] == 0                          # brightness byte forced dark
    # a pixel lit only on warm-white still counts as lit
    dmx2 = bytearray(512)
    ab.wrap_tube(dmx2, 1, [(0, 0, 0, 0, 99)] + [(0, 0, 0, 0, 0)] * 31)
    assert dmx2[3] == 255


# --- build_artdmx <-> parse_artdmx round trip -----------------------------

def test_build_artdmx_roundtrips_through_parser():
    data = bytes([1, 2, 3, 4, 5])
    pkt = ab.build_artdmx(258, data, seq=7)     # 258 = net 1, sub_uni 2
    assert artnet.parse_artdmx(pkt) == (258, [1, 2, 3, 4, 5])


# --- parse_tubes validation ----------------------------------------------

def _cfg(**over):
    base = {"in_universe": 0, "in_address": 1, "out_universe": 0, "out_address": 1}
    base.update(over)
    return {"tube": {"t": base}}


def test_parse_tubes_ok():
    tubes = ab.parse_tubes(_cfg(), "rgb", 32)
    assert len(tubes) == 1 and tubes[0].out_address == 1


def test_parse_tubes_overrun_rejected():
    # addr 300 + (2 + 32*7 = 226) - 1 = 525 > 512
    with pytest.raises(ValueError):
        ab.parse_tubes(_cfg(out_address=300), "rgb", 32)


def test_parse_tubes_missing_key_rejected():
    with pytest.raises(ValueError):
        ab.parse_tubes({"tube": {"t": {"in_universe": 0}}}, "rgb", 32)


def test_parse_tubes_per_tube_personality_overrides_default():
    cfg = {"tube": {
        "a": {"in_universe": 0, "in_address": 1, "out_universe": 0, "out_address": 1},
        "b": {"in_universe": 1, "in_address": 1, "out_universe": 0, "out_address": 227,
              "personality": "rgbaw"},
    }}
    by = {t.name: t for t in ab.parse_tubes(cfg, "rgbw", 32)}
    assert by["a"].personality == "rgbw"        # inherits the module default
    assert by["b"].personality == "rgbaw"       # tube override wins


def test_parse_tubes_unknown_personality_rejected():
    with pytest.raises(ValueError):
        ab.parse_tubes(_cfg(personality="rgbxyz"), "rgb", 32)


def test_parse_tubes_input_overrun_rejected():
    # rgbw = 4 ch/px * 32 = 128 input ch; in_address 400 -> 400+128-1 = 527 > 512
    with pytest.raises(ValueError):
        ab.parse_tubes(_cfg(in_address=400), "rgbw", 32)


# --- render_universe ------------------------------------------------------

def test_render_universe_places_two_tubes():
    cfg = {"tube": {
        "a": {"in_universe": 0, "in_address": 1, "out_universe": 0, "out_address": 1},
        "b": {"in_universe": 0, "in_address": 97, "out_universe": 0, "out_address": 227},
    }}
    tubes = ab.parse_tubes(cfg, "rgb", 32)
    # input universe: tube a pixel0 = red at ch1..3, tube b pixel0 = green at ch97..99
    src = [0] * 512
    src[0], src[1], src[2] = 255, 0, 0          # a.px0 red
    src[96], src[97], src[98] = 0, 255, 0       # b.px0 green
    dmx = ab.render_universe(0, tubes, {0: src})
    assert dmx[226] == ab.PIXEL_MODE_VAL        # tube b block present at 227
    # tube a block at ch1: [mode, count, colour-mode, bri, R, G, B, ...] -> RGB at index 4
    assert list(dmx[4:7]) == [255, 0, 0]        # tube a px0 RGB
    # tube b block at ch227 (index 226): RGB at index 226+4 = 230
    assert list(dmx[230:233]) == [0, 255, 0]    # tube b px0 RGB


def test_render_universe_dark_when_no_input():
    tubes = ab.parse_tubes(_cfg(), "rgb", 32)
    dmx = ab.render_universe(0, tubes, {})       # no input yet
    assert dmx[0] == ab.PIXEL_MODE_VAL          # still frames the tube (mode/count set)
    assert dmx[3] == 0                           # but every pixel dark
