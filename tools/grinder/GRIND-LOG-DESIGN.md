# Grind-log design (deferred — note only, not built)

User changes grind rarely and doesn't chase one-off shots due to
retention. So instead of per-shot grind entry, record **grind
*changes*** as discrete events and inherit forward.

## Data

`shots/grind_log.json` — append-only JSON array on refuge, each entry:

```json
{ "ts_utc": "2026-10-31T04:12:00Z", "setting": 122, "note": "new bag, went coarser" }
```

## Input methods (pick any, all three write to the same file)

1. **Dashboard button** — small "+Grind change" chip in the Recent-shots
   header; one-field dialog captures the new setting (+ optional note).
   Zero friction when at the Mac. Mutates grind_log.json via a tiny
   endpoint added to the stack (or just writes direct via the existing
   logger).
2. **iOS Shortcut over SSH** — same auth path as `coffee-fram-sync`.
   One-tap home-screen icon prompts for a number, SSH into refuge,
   append entry.
3. **CLI** — `grind-set 122 [optional note]`. Muscle-memory command
   when terminal is open.

## Backfill script

Walk shots chronologically. For each shot, assign
`metadata.grinder_setting` = latest grind_log entry whose `ts_utc` ≤
shot's pump_on. Idempotent (same pattern as `backfill.py`). Index
rebuild triggers via `docker restart shot-logger`.

## Dashboard surfacing

- Row chip: show `grinder_setting` next to `ground_weight_g`
- Header: "Current grind: 122 (changed 2026-10-31)"
- RI calculation stays as-is; the setting is informational

## When to build

After enough grind-change events have accumulated or when operator
decides it's time. One-shot build; the design is simple enough that
reaching for it later is easy. Nothing automated — manual input only.
