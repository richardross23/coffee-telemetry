# tools/

Scripts that aren't part of the running stack but are useful around it.

## `grinder/` — Mahlkönig E65S GBW

The grinder exposes a diagnostic WiFi AP (owner mode only — hold both
buttons while powering on) that serves recent dose history over
unauthenticated HTTP at `http://192.168.4.1`. These tools pull that
log and backfill it into the espresso shot archive.

| Script | What it does |
|---|---|
| `coffee-fram-sync` | **Everyday command.** Shell wrapper run from a Mac that's on the grinder's AP (WiFi) and refuge (ethernet). Pulls `fram.bin`, scps to refuge, invokes `backfill.py`. |
| `coffee-fram-sync-poll` | **Automated version.** launchd-fired every 15 minutes; silently joins the grinder AP if visible, runs the sync, restores prior WiFi. Debounced to one real sync per 12h. See `com.rwross.coffee-fram-sync-poll.plist`. |
| `backfill.py` | Parses a `fram.bin`, matches each grinder record to a shot by timestamp (±24h, chronological 1-to-1), writes `ground_weight_g` + `grind_duration_s` into shot metadata, rebuilds the dashboard index. Idempotent. |
| `fetch_fram.py` | Standalone FRAM downloader (POST /stm?framFetch → poll /status → GET /fram.bin). Used by `coffee-fram-sync` conceptually; usable directly if you want just the file. |
| `decode_fram.py` | Converts a `fram.bin` to the text log format `import_grinder_log.py` expects. Legacy — `backfill.py` reads FRAM directly. |
| `import_grinder_log.py` | Legacy pipeline: parses a text log paste from the grinder's `/logs?Shots` endpoint and publishes updates via MQTT. Kept for anyone preferring that flow. |
| `probe_grinder.py` | Walks the grinder's HTTP endpoints and prints what each returns. |

### Typical use (every few weeks, or whenever stats need to be fresh)

1. Power the grinder on in owner mode (hold both buttons during power-on).
2. On the Mac, join its WiFi to the grinder's SSID. Ethernet stays on
   main LAN so refuge is still reachable.
3. Run `coffee-fram-sync`. It will take ~70s (dominated by the ~65s
   FRAM regen on the grinder).
4. Reload the dashboard. Row chips will show e.g. `19.78g ground`
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

### Automated variant

If you don't want to think about it, install the launchd watcher:

```
cp coffee-fram-sync-poll ~/.local/bin/
cp com.rwross.coffee-fram-sync-poll.plist ~/Library/LaunchAgents/
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.rwross.coffee-fram-sync-poll.plist
```

It fires every 15 min. The grinder AP must be in range and in owner mode
for the join to succeed. Logs land in `~/Library/Logs/coffee-fram-sync-poll.log`.

To stop:

```
launchctl bootout "gui/$(id -u)/com.rwross.coffee-fram-sync-poll"
```
