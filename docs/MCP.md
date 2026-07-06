# neewer-mcp — MCP server

`neewer-mcp` exposes a running `neewerd` daemon to AI assistants over the
[Model Context Protocol](https://modelcontextprotocol.io). An assistant (Claude
Desktop, etc.) can then drive the lights conversationally: "set the key lights
warm at 40%", "start a palette flow", "what's the battery on tube 2?".

It is a **client of the daemon**, not a second Bluetooth central. It talks to the
daemon's `/api/v1` HTTP layer and never touches BLE, so `core` remains the only
owner of the radio. Every tool composes one command-grammar line and sends it
through the same `core.dispatch` every other transport uses — there is one grammar.

## Setup

```sh
pip install '.[mcp]'          # installs the `mcp` SDK + the `neewer-mcp` script
```

Enable the daemon's HTTP module (the MCP server's transport) and bind it to
loopback — the API has no auth or TLS:

```toml
# neewerd.toml
[modules.http]
enabled = true
host = "127.0.0.1"
port = 8099
```

Run the daemon (`neewerd neewerd.toml`), then point an MCP client at `neewer-mcp`.

### Claude Desktop config

```json
{
  "mcpServers": {
    "neewer": {
      "command": "neewer-mcp",
      "env": { "NEEWER_MCP_URL": "http://127.0.0.1:8099" }
    }
  }
}
```

The daemon endpoint is resolved as `--http-url` > `$NEEWER_MCP_URL` >
`http://127.0.0.1:8099`.

## Tools

Targets are `all` · `t<N>` (physical position) · a group/alias from the device
book (`devices.example.toml`) · a MAC — whatever `core.resolve()` accepts.

| Tool | Params | Does |
|---|---|---|
| `list_lights` | — | known tubes: target, name, position, connected |
| `get_state` | `target="all"` | cached per-tube state (power, colour if known, battery) |
| `power` | `target, on` | on / off |
| `set_hsi` | `target, h, s, i` | hue 0-359, sat 0-100, intensity 0-100 |
| `set_cct` | `target, bri, temp, gm=50` | bri 0-100, temp 32-85 (×100 K), GM 0-100 (50 neutral) |
| `set_brightness` | `target, bri` | brightness only (neutral white) |
| `set_rgbcw` | `target, bri, r=0, g=0, b=0, c=0, w=0` | TL120C by-MAC: rgb 0-255 + dedicated cold/warm white 0-255 |
| `set_xy` | `target, bri, x, y` | TL120C by-MAC: CIE-1931 chromaticity, x/y 0.0-1.0 |
| `set_gel` | `target, hue, sat, bri, brand="rosco", gel_no=0` | TL120C by-MAC gel/colour-paper: HSI + brand (rosco/lee) + catalog no. |
| `scene` | `target, effect, params=[]` | built-in scene by id |
| `start_flow` | `mode, opts={}` | animation: hue/comet/palette/tri/multistop, e.g. `{"speed":"0.05"}` |
| `stop` | — | stop any running flow |
| `query_status` | `target="all"` | ask tubes for battery/state/version (read `neewer://state` after) |
| `list_presets` | — | configured presets (name → command lines) |
| `run_preset` | `name` | run a named preset |

A daemon-side error (unknown target/preset, bad args) or an unreachable daemon
comes back as an MCP **tool error** carrying the daemon's message; successes
return the human `ok …` reply string.

## Resources

| URI | Content |
|---|---|
| `neewer://state` | full live snapshot JSON (all tubes) — read state without a tool call |
| `neewer://presets` | configured presets JSON, for discovery |

## Testing / inspecting

```sh
# interactive: the MCP Inspector against the running server
npx @modelcontextprotocol/inspector neewer-mcp
```

The unit tests (`tests/test_mcp_server.py`) run hardware-free with only pytest —
the translation/transport helpers import nothing from the `mcp` SDK, so CI covers
them without installing the extra (only the SDK-wiring test skips when `mcp` is
absent).
