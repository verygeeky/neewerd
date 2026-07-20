# Changelog

All notable changes to the `neewerd` daemon are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/); pre-1.0, a minor bump may change
behaviour.

## [0.1.0] — unreleased

A thin I/O daemon over the [`neewer`](https://pypi.org/project/neewer/) library.

### Added
- I/O modules — `socket`, `http` (REST `/api/v1` + legacy + bundled UIs), `mqtt`
  (Home Assistant discovery), `osc`, `artnet`, `sacn` — plus the `neewer-mcp`
  stdio MCP server.
- **`artnet_bridge` module.** Receives a plain pixel stream (RGB, RGBW, RGBCW, or
  RGBAW/RGBWA, N channels per pixel) and re-sends it as Art-Net in the TL120C
  32-pixel-custom personality, unicast to an Art-Net (DMX-over-IP) node that outputs
  wired DMX512 to the tubes. The source is set up as an ordinary strip; the module
  writes the mode and pixel-count header, the seven-channels-per-pixel RGBCW layout,
  and the cold/warm-white mapping. `rgbaw`/`rgbwa` map a console fixture's white and
  amber onto the tube's cold and warm white, for approximate colour temperature from a
  source with no dedicated cold/warm split. It does no BLE work: Art-Net in, Art-Net
  out. Each tube maps one input slice `(in_universe, in_address)` to one output slice
  `(out_universe, out_address)` under `[modules.artnet_bridge.tube.<name>]`, and may
  set its own `personality`/`pixels` (otherwise it inherits the module default), so one
  rig can mix front-end formats or bin-pack several tubes into a universe. The loader
  rejects any tube whose input or output slice runs past channel 512. The config surface
  is documented in `neewerd.example.toml`, with worked examples in `examples/`.
- **`neewerd.client.DaemonClient`** — the shared daemon HTTP client, promoted out
  of the MCP module so `neewer-mcp` (and any script / a future `neewerctl` HTTP
  mode) consume one client, not three.
- **Config-schema validation** — unknown top-level / `[core]` / `[modules.*]` keys
  are warned at startup instead of silently dropped (catches typos).
- **`py.typed`** marker so type hints ship.
- The **Art-Net** module now supports the `rgb` and `rgbw` DMX personalities, so
  you can drive tubes straight from RGB/RGBW Art-Net sources such as LedFx. It
  also logs periodic per-tube write-rate telemetry (`artnet perf`).
- The **Art-Net** module now paces BLE writes with the library's per-connection
  write governor, so a fast source (a music-reactive rig at high frame rates)
  can't back up the Bluetooth transmit queue. Its periodic log line and the state
  API now carry per-tube pacing telemetry (rate / bandwidth / latency / deferred).
  The pacing bounds are optional config knobs — see `neewerd.example.toml` — and
  auto-tune with zero config.
- **`/console` telemetry** — the state API (`GET /api/v1/state` and the SSE
  stream) now folds per-tube write-pacing stats under a `gov` key (an enriched
  snapshot). The bundled `/console` page shows full per-light detail (model,
  firmware, battery, temperature, RSSI, network id, last command) alongside a
  governor block and a system panel with a backpressure indicator.
- **Presets** are registered as a command verb via the library's `register_verb`
  hook (preset storage lives in the daemon, not the library).
- **Runtime signals** — tune a running daemon without dropping the BLE links:
  `SIGUSR1` toggles DEBUG logging, `SIGUSR2` resets to the configured level, and
  `SIGHUP` re-reads the config and hot-applies the safe subset (`[presets]`,
  `[core.positions]` — restamped onto live tubes), logging what it ignored.
- **`[core] liveness_interval`** — passes the library's half-open-link liveness
  probe threshold through (default 30 s, `0` disables); see `neewerd.example.toml`.

### Changed
- **HTTP status mapping** now keys off the library's typed `neewer.errors` types
  (`UnknownTarget`/`UnknownPreset` → 404, `Unsupported` → 422,
  `UnknownAction`/`UnknownEffect` → 400) — the old reply-string prefix-sniffing is
  gone. A command that **no** addressed fixture supports is now **422** (was a
  200-with-detail); a *partial* success stays 200.
- **SSE** (`/api/v1/events`) now **pushes on change** via the library's change-event
  API; the interval is a keepalive heartbeat, not a poll.
- **HTTP `/api/v1` field mapping** now derives argument order from the library's
  `commands.ACTIONS` registry (one source of truth) instead of a local table.
- Pinned the library dependency to `neewer>=0.1.0,<0.2.0`.
- `osc` module imports the grammar from `neewer.grammar` (was `neewer.protocol.commands`).
