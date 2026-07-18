# neewerd

Thin, library-backed control **daemon** for Neewer TL90C / TL120C ("NEEWER
Infinity") RGB tube lights.

`neewerd` is built on the
[`neewer`](https://github.com/verygeeky/neewer-python) library and is based on
the research documented in the
[neewer-hardware](https://github.com/verygeeky/neewer-hardware) reference.

This package is the daemon *shell* only. All the byte-wise protocol knowledge,
the `Fleet` BLE client, animation effects, the device book and the BlueZ
self-heal live in the [`neewer`](https://github.com/verygeeky/neewer-python)
library — `neewerd` depends on it (pinned `neewer>=0.1.0,<0.2.0`) and just wires
pluggable I/O front-ends (socket / MQTT / OSC / HTTP+web UI / Art-Net / sACN)
and an MCP server onto the library's `Fleet`.

```
python -m neewerd [config.toml]
```

New protocol verbs (rgbcw / xy / gel by-MAC colour, …) arrive **for free** the
moment the library gains them: the daemon adds no per-verb code, only HTTP
field-maps and MCP tool wrappers. On startup the config is validated — unknown
top-level / `[core]` / `[modules.*]` keys are warned (a typo won't silently drop
a setting).

## Console scripts

- `neewerd` — the daemon itself.
- `neewerctl` — a thin client of the daemon's command socket.
- `neewer-mcp` — a stdio MCP server; a *client* of a running daemon's HTTP API
  (installed by `pip install 'neewerd[mcp]'`).

## Layout

- `neewerd/__main__.py` — entrypoint: load + validate config → start `neewer.Fleet` → start modules.
- `neewerd/modules/` — I/O front-ends (`socket`, `mqtt`, `osc`, `http`, `artnet`, `sacn`) + the bundled web UIs. The `artnet` module drives tubes from DMX-over-IP with four personalities (`hsi` / `cct` / `rgb` / `rgbw`), so RGB/RGBW sources such as LedFx work directly; it paces BLE writes with a per-connection governor (auto-tuning, zero-config) so a fast source can't back up the Bluetooth transmit queue, and reports per-tube pacing telemetry (rate / bandwidth / latency / deferred).
- `neewerd/client.py` — `DaemonClient`: the shared async client of the daemon's `/api/v1` HTTP layer (no `bleak`, no `neewer` BLE code). Consumed by `neewer-mcp`.
- `neewerd/mcp_server.py` — the MCP tools, built on `DaemonClient`.
- `neewerd/socketpath.py` — shared command-socket path resolver.

## HTTP API

The `http` module serves a REST layer at `/api/v1`, a legacy line transport, a
Server-Sent-Events stream that **pushes on change** (`/api/v1/events`), and the
bundled web UIs (`/`, `/console`). The `/console` page shows full per-light detail
(model / firmware / battery / temperature / RSSI / network id / last command) plus
a write-pacing (governor) block and a system panel with a backpressure indicator;
the state API and SSE stream carry the same per-tube pacing stats under a `gov`
key. Full reference: [`docs/HTTP-API.md`](docs/HTTP-API.md).
Bind loopback — there is no auth/TLS.

## Runtime signals

Tune a running daemon without a restart (a restart drops every BLE link):

- `SIGUSR1` — toggle DEBUG logging on/off (watch the notify/GATT stream live).
- `SIGUSR2` — reset the log level to the configured one.
- `SIGHUP` — re-read the config and hot-apply the safe subset: `[presets]` and
  `[core.positions]`. Roster keys and module knobs are read at startup only;
  the reload logs exactly what it ignored.

```
kill -USR1 $(pgrep -f 'python -m neewerd')
```

## Install (local dev)

```
uv venv .venv
.venv/bin/pip install -e '.[all]'
```

(Or, with a sibling checkout of
[neewer-python](https://github.com/verygeeky/neewer-python) to develop both at
once: `.venv/bin/pip install -e ../neewer-python -e '.[all]'`.)

### Windows

Installation works on Windows with the same steps. The daemon runs successfully
and communicates with Neewer lights via Bluetooth LE through the `bleak` library's
WinRT backend.

**Windows-specific notes:**

1. **Console scripts PATH**: After `pip install`, the scripts (`neewerd`,
   `neewerctl`, `neewer-mcp`) are installed to
   `%APPDATA%\Python\Python3XX\Scripts`. If this directory is not on your PATH,
   either add it or run the daemon via the module:
   ```
   python -m neewerd [config.toml]
   python -m neewerd --help
   ```

2. **Runtime signal handlers**: The Unix signals `SIGUSR1`, `SIGUSR2`, and
   `SIGHUP` (for toggling log levels and hot-reloading config) are not available
   on Windows. These features are gracefully disabled on Windows. To change log
   levels or reload config on Windows, restart the daemon or use the HTTP API if
   the `http` module is enabled.

3. **Config file location**: The default config search path includes
   `./neewerd.toml` (current directory) and checks the `NEEWERD_CONFIG`
   environment variable. The `/etc/neewerd/neewerd.toml` path is Unix-specific
   and won't be checked on Windows.

4. **Example config**: Copy `neewerd.example.toml` to `neewerd.toml` and enable
   the modules you want (at minimum, enable `[modules.socket]` or
   `[modules.http]` for control access).

See [`CHANGELOG.md`](CHANGELOG.md) for notable changes.
