# DDD investigation — closed

The Mahlkönig E65S GBW markets Disc Distance Detection (DDD). We looked
at whether the WiFi diagnostic module exposes it so we could backfill
per-shot grind-position into the espresso archive alongside dose and
grind time.

**Conclusion: it doesn't. Don't re-investigate.**

## What we checked (2026-10-31, firmware 3.0 build 2021-09-21)

- Full 256 KB FRAM dump — only the shot-log region (0x1080–0x76e8) and
  a settings zone around 0x000c4 are usefully populated; two more
  structured zones at 0x11080 and 0x15080 whose contents don't correlate
  with per-shot DDD (they look like event / telemetry logs with their
  own schema).
- Per-shot record layout (29 bytes): flag + ts + target_g + actual_g +
  grind_ms. Every other byte in the record is zero across all 904
  records. No position field.
- Grinder HTTP endpoints:
  - `/info` — version, serial, model
  - `/status` — firmware state, FatFs stats, framProgress, uart state
  - `/stats` — lifetime + resettable counters (truncated server-side at
    1019 bytes); includes `discTime` which equals `motorRunTime` /
    `grindTime`, so it's engagement time, not position
  - `/fram.bin` — the FRAM dump
  - Everything else we tried (`/sensors`, `/state`, `/diagnostics`,
    `/settings`, `/ddd`, `/measurement`, `/calibration`, `/burr`,
    `/debug`, `/motor`, `/scale`, …) returns HTTP 500 or 404
- SPA code (`app.js` + `chunk-vendors.js`) grepped for every
  DDD-adjacent identifier (`disc`, `distance`, `burr`, `gap`, `ddd`,
  `sensor`, `calib`, `coarse`, `fine`, `position`). Hits only reference
  the display formatting of `discTime` from `/stats`.

## Architectural reason

The grinder has two controllers. The main controller does the grinding,
reads the DDD sensor, drives the display, and knows the dial position
in real time. The auxiliary WiFi module (ESP32-based, added for firmware
updates and the dose log) connects to the main controller via UART
(visible in `/status.grinderUartState`). The main controller sends only
a subset of its data over UART: grind events, timestamps, weights,
cumulative counters. DDD readings aren't on that bus.

## Remaining paths to capture DDD

1. Manual per-change logging on the dashboard (user doesn't want this).
2. UART sniff with a logic analyzer (invasive, probably not worth it).
3. Future Mahlkönig firmware that exposes DDD over HTTP (keep an eye out).

## Useful bonus from `/stats`

The endpoint is undocumented but turns out to be a goldmine of lifetime
counters that are NOT in the FRAM shot log:

- `shots` — lifetime shot count (3164 at probe time; FRAM only holds the
  recent 904 due to ring-buffer)
- `grindWeight`, `grindTime`, `motorRunTime`, `motorOns`, `restarts`
- Per-recipe (`r1_` through `r6_`) weight / time / shot counts
- Resettable counters prefixed `r` (e.g. `rshots`, `rgrindWeight`)

If we ever want burr-wear tracking or per-recipe-load reporting,
`/stats` is where it comes from.

## Baseline snapshot

A full capture of the grinder's HTTP response surface + a FRAM snapshot
is preserved at `/volume2/docker/coffee/tools/grinder/probes/20261031T040232Z/`
on refuge. If a future firmware adds DDD, we can diff a fresh probe
against this baseline to find the new field quickly.
