# artnet_bridge configs

`artnet_bridge` takes a simple pixel stream (plain RGB/RGBW/... Art-Net) and re-emits it in the
tube's DMX personality to an Art-Net (DMX-over-IP) node. You describe the rig as a set of tubes,
and each tube maps an input slice to an output slice.

## [modules.artnet_bridge]

| key | default | meaning |
|---|---|---|
| `listen_host` / `listen_port` | `0.0.0.0` / `6454` | where the pixel stream arrives |
| `dest` / `dest_port` | required / `6454` | the node's IP (unicast) |
| `fps` | `44.0` | output refresh. Nodes blackout on timeout, so frames resend steadily. |
| `personality` | `rgb` | module default input format (see below). A tube can override it. |
| `pixels` | `32` | module default pixel count. A tube can override it. |

## Personalities

Each personality sets how many input channels a pixel carries and how they land on the tube's
`(R, G, B, cold-white, warm-white)`.

| name | ch/px | source order | mapping |
|---|---|---|---|
| `rgb`   | 3 | `[R,G,B]`       | no white (CW and WW stay 0) |
| `rgbw`  | 4 | `[R,G,B,W]`     | one white drives both emitters (neutral: CW = WW = W) |
| `rgbcw` | 5 | `[R,G,B,CW,WW]` | straight through, full cold/warm control |
| `rgbaw` | 5 | `[R,G,B,A,W]`   | White to cold-white, Amber to warm-white (approximate CCT from a stock RGBAW fixture) |
| `rgbwa` | 5 | `[R,G,B,W,A]`   | same as rgbaw, for sources that put white before amber |

## [modules.artnet_bridge.tube.name]

| key | required | meaning |
|---|---|---|
| `in_universe` / `in_address` | yes | input slice: universe plus 1-based start channel of pixel 0 |
| `out_universe` / `out_address` | yes | output slice: universe plus 1-based start of the tube's block |
| `personality` | no, falls back to the module default | this tube's input format |
| `pixels` | no, falls back to the module default | this tube's pixel count |

The output block is `2 + pixels * 7` channels (226 for a 32-pixel tube), so two tubes fit one
output universe at addresses 1 and 227. The input block is `pixels * stride`. The loader rejects
any tube whose input or output slice runs past channel 512, so a mis-patch fails at startup
rather than corrupting a frame mid-show. Personality and pixels are set per tube, so one rig can
pack an rgbw tube next to an rgbaw one, or run different pixel counts on different ports.

## Examples

`rgbaw-uni-per-tube.toml` puts one tube on each Art-Net universe with an RGBAW front-end (White
and Amber become cold and warm white). It is the easiest to reason about and leaves plenty of
room on every universe.

`rgbw-binpacked.toml` packs all four tubes into one universe. RGBW is 128 channels per tube, so
four tubes land at 1, 129, 257, 385 and fill 512 exactly. Use it when you would rather not spend
a universe per tube.

Run: `python -m neewerd examples/rgbw-binpacked.toml`
