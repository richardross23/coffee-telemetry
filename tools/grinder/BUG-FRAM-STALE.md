# Known Mahlkönig bug: stale FRAM dose log

**Observed 2026-11-01.** The grinder's own web interface and `/fram.bin`
dump suddenly started returning dose records from a 2025 window
(2025-08-20 → 2025-12-05, 264 records) instead of the ~900 2026 records
that had been there the day before. The RTC is correct — the general
settings screen shows the right day/month/year and time — but the shot
log stopped updating with current activity.

Not a timezone / parser issue: raw bytes verified. `rec[5:9]` little-endian
uint32 reads e.g. `1755769662` = `2025-08-21T09:47:42Z`, matching real
grind weights + durations.

## What's affected

- `coffee-fram-sync` runs successfully (grinder HTTP responds, regen
  completes, download works after the usual retry) but the backfill
  matches 0 shots because the record timestamps don't fall near any
  real 2026 shot.
- Per-shot `ground_weight_g` and `grind_duration_s` can't be backfilled
  until the grinder is logging current activity again.

## What's NOT affected

- Sticky metadata on new shots. `grinder_setting`, `roast_date`, and
  `bean_type` ride forward via logger.py's `latest_metadata()`
  inheritance. The New Bag / Grind Change iOS Shortcuts call
  `set_sticky.py` on refuge, which updates the latest shot — next
  shot inherits it.
- Dashboard RI, chip layout, everything else.

## Workaround

Don't run FRAM sync until the grinder un-sticks itself. The iOS Shortcut
still works mechanically, it just produces 0 matches.

Operator plan: power the grinder off overnight and see whether the
dose-log recovers. If it does, re-run FRAM sync and the next ~day of
real 2026 grinds will backfill cleanly. If it doesn't, Mahlkönig
support territory.

## If this recurs

Three options for making the 264 (or N) 2025-dated records usable:

1. Compute a per-run offset (`today - max(fram_date)` ≈ constant) and
   shift all records by that offset. Fragile and hides the real bug.
2. Match by grind weight + sequence rather than timestamp. Possible —
   grinds are highly stereotyped (18–22 g) but shot↔grind pairing was
   designed around timestamps for a reason.
3. Shrug and move on; sticky metadata still gives you per-shot
   grinder_setting via the Shortcut flow.

Default to option 3 unless the grinder stays broken for weeks.

## Diagnosis commands

```bash
# Pull FRAM now
coffee-fram-sync-oneshot

# Dry-run backfill with current fram.bin
ssh refuge python3 /volume2/docker/coffee/tools/grinder/backfill.py \
    --fram-bin /tmp/fram.bin --dry-run

# Inspect raw bytes if timestamps look wrong
ssh refuge "python3 -c '
import struct, datetime
with open(\"/tmp/fram.bin\",\"rb\") as f: buf=f.read()
for i in range(5):
    off = 0x1080 + i*29
    ts = struct.unpack(\"<I\", buf[off+5:off+9])[0]
    print(i, ts, datetime.datetime.fromtimestamp(ts, datetime.timezone.utc))
'"
```
