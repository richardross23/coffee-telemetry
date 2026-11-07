# tools/

Scripts that aren't part of the running stack but are useful around it.

## `grinder/` — Mahlkönig E65S GBW

The grinder exposes a diagnostic WiFi AP (owner mode only — hold both
buttons while powering on) that serves recent dose history over
unauthenticated HTTP at `http://192.168.4.1`. These tools pull that
log and backfill it into the espresso shot archive.

| Script | What it does |
|---|---|
| `coffee-fram-sync-oneshot` | **One-touch command.** Joins the grinder AP, invokes `coffee-fram-sync`, restores prior WiFi. Designed to run inside the grinder's ~2-minute no-client AP timeout. Bind to a macOS Shortcut or iOS SSH shortcut for literal one-tap. |
| `coffee-fram-sync` | **Lower-level command.** Shell wrapper run from a Mac that's on the grinder's AP (WiFi) and refuge (ethernet). Pulls `fram.bin`, scps to refuge, invokes `backfill.py`. |
| `backfill.py` | Parses a `fram.bin`, matches each grinder record to a shot by timestamp (±24h, chronological 1-to-1), writes `ground_weight_g` + `grind_duration_s` into shot metadata (plus `grinder_setting` from the measured disc distance, on patched firmware), rebuilds the dashboard index. Idempotent. Tests: `python3 grinder/test_backfill.py`. |
| `fetch_fram.py` | Standalone FRAM downloader (POST /stm?framFetch → poll /status → GET /fram.bin). Used by `coffee-fram-sync` conceptually; usable directly if you want just the file. |
| `decode_fram.py` | Converts a `fram.bin` to the text log format `import_grinder_log.py` expects. Legacy — `backfill.py` reads FRAM directly. |
| `import_grinder_log.py` | Legacy pipeline: parses a text log paste from the grinder's `/logs?Shots` endpoint and publishes updates via MQTT. Kept for anyone preferring that flow. |
| `probe_grinder.py` | Walks the grinder's HTTP endpoints and prints what each returns. |

### Typical use

1. Power the grinder on in owner mode (hold both buttons during power-on).
2. Trigger the sync: either `coffee-fram-sync-oneshot` from a terminal, a
   macOS Shortcut you've bound to a menu-bar icon or hotkey, or an iOS
   Shortcut that runs it over SSH. The whole cycle takes ~70s and must
   complete before the grinder's ~2-minute no-client AP timeout — the
   oneshot script joins the AP first so the clock starts then.
3. Reload the dashboard. Row chips will show e.g. `19.78g ground`
   / `5.38s grind` instead of the plain dose label.


### Grinder quirks the tooling absorbs

- The RTC does NOT track DST. Timestamps are interpreted as fixed AEST
  (UTC+10) regardless of the real-world DST state.
- The RTC drifts a few minutes per month. The ±24h match window
  tolerates this; `backfill.py` prints a per-month offset report so
  the drift is visible.
- Normal power-off kills the WiFi AP. Owner-mode button-hold must be
  repeated on each power-on. This is why the workflow is manual rather
  than scheduled.
- The AP is single-radio and the ESP32 web server slows under load;
  one request at a time, don't hammer it.