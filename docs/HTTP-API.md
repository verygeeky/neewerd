# neewerd HTTP API (`/api/v1`)

The `http` module serves a small REST layer plus a legacy line transport and the
bundled web UIs. Enable it with `[modules.http] enabled = true` (binds
`127.0.0.1` by default — there is **no auth/TLS**, so keep it on loopback unless
you know what you're doing).

Base URL in examples: `http://127.0.0.1:8099`.

## Commands — `POST /api/v1/<target>/<action>`

`<target>` is `all`, `t<N>` (physical position), a device-book alias/group, or a
MAC. The JSON body carries the action's arguments by name (the field order per
action mirrors the typed command model in `neewer.protocol.commands`):

| Action  | Body                                             |
|---------|--------------------------------------------------|
| `power` | `{"on": true}`                                   |
| `hsi`   | `{"h": 240, "s": 100, "i": 80}`                  |
| `cct`   | `{"bri": 80, "temp": 56, "gm": 50}`              |
| `bri`   | `{"bri": 80}`                                     |
| `scene` | `{"effect": 3, "params": [9]}`                    |
| `pixel` | `{"colors": ["0", "off", "240"]}`                |
| `rgbcw` | `{"bri": 50, "r": 0, "g": 127, "b": 250, "c": 0, "w": 0}` |
| `xy`    | `{"bri": 50, "x": 0.3127, "y": 0.3290}`          |
| `gel`   | `{"hue": 45, "sat": 100, "bri": 50, "brand": "lee", "gel_no": 7}` |
| `identify` | *(no body)*                                   |

Alternatives accepted by every action: `{"args": [ ... ]}` (positional), a bare
JSON array, or `{"cmd": "all hsi 240 100 80"}` (full grammar-line escape hatch —
also `POST /api/v1/command`).

Response: `200 {"result": "ok hsi -> 2 tube(s)"}` on success, else
`{"error": "..."}` with a status code:

| Status | Meaning                                                        |
|--------|---------------------------------------------------------------|
| `200`  | applied (a *partial* apply — some tubes skipped — is still 200, with detail) |
| `400`  | malformed arguments, or an unknown action/effect              |
| `404`  | the target resolved to no connected tubes, or no such preset  |
| `422`  | well-formed, but **no** addressed fixture supports the command |
| `413`  | request body over 64 KiB                                       |
| `500`  | unexpected server error                                       |

## Reads

- `GET /api/v1/state` — per-tube state snapshot (JSON). `GET /api/v1/<target>/state` filters.
- `GET /api/v1/presets` — configured presets (name → command lines).
- `GET /api/v1/events` (alias `/events`) — Server-Sent-Events stream that pushes
  the state snapshot **on change** (connect/disconnect/telemetry/command), with a
  periodic keepalive. `event: state`, `data: <json>`.
- `POST /api/v1/preset/<name>` — run a preset (404 if unknown).

## Legacy line transport

- `POST /cmd` with a raw command line body (`all hsi 240 100 80`) → `text/plain` reply.
- `GET /state` → JSON snapshot.
- `GET /all/power/off` — path-as-command fallback.

## UIs

- `GET /` — basic control page. `GET /console` — the richer console (SSE-driven).
