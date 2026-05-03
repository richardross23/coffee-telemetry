# tools/

Scripts that aren't part of the running stack but are useful around it.

## `grinder/` — Mahlkönig E65S GBW

Reverse-engineering helpers for the Mahlkönig E65S GBW grinder, which exposes
recent dose history over an unauthenticated WiFi AP. Skip this directory
unless you have that grinder.

| Script | What it does |
|---|---|
| `probe_grinder.py` | Walks the grinder's HTTP endpoints and prints what each returns. Starting point for figuring out what's available. |
| `fetch_fram.py` | Pulls the FRAM dump (recent shots ring buffer) over HTTP. |
| `decode_fram.py` | Parses a FRAM dump into the 29-byte records the grinder stores per shot. |
| `import_grinder_log.py` | Reads the shot log the grinder writes and merges `ground_weight_g`, `grind_duration_s`, and `grinder_recipe` into the matching `coffee-telemetry` shot files (matched by timestamp). |

`import_grinder_log.py` is the only one most people will care about — once
your grinder log is on disk, it backfills every shot file with the precise
ground weight (vs the manual dose entry on the dashboard).
