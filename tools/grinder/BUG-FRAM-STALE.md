# "Frozen" FRAM dose log: a partial download, not a grinder bug

**Resolved 2026-11-08.** The grinder never stopped logging. `coffee-fram-sync`
shipped a partial dump to the backfill.

## What happened (2026-11-01, 01:35)

```
► POST /stm?framFetch
► waiting for framProgress=100 (expect ~65s)…
  framProgress=100%
► downloading /fram.bin
     12288 bytes
```

`framProgress` still read 100 from the previous fetch, so the script downloaded
`/fram.bin` at once, while the new dump was being written. It got 12,288 of
262,144 bytes and passed a 10,000-byte check. The start of the shot log held
2025 records, so backfill parsed 264 records (2025-08-20 → 2025-12-05) and
matched no 2026 shots.

The log itself was healthy: the 2026-11-08 dump has records on every day from
2026-10-31 to 2026-11-08, with no gap.

## Fix

- `coffee-fram-sync` only trusts a 100 that follows a lower reading, and
  requires exactly 262,144 bytes.
- `backfill.py` refuses a file that stops before the end of the shot log
  (0x9080). Backfill also clears values from shots that lose their match, so a
  partial dump would now remove data rather than just match nothing.

`fetch_fram.py` still trusts the first 100 it sees; backfill's check catches a
short file from it.

## Things that look similar

- `decode_fram.py` used to match `01 00 00 00 08`, so it saw only records whose
  preceding flags byte was 1, and looked stuck in old dates. Fixed 2026-11-08.
- The grinder's web log lists the oldest record first, so 2025 rows at the top
  are normal.

## Firmware: the log reader doesn't wrap

From reading the SW300 image; predicted, not observed on the grinder.

`/logs?Shots` starts at the oldest record (ring control at FRAM 0x1000: 12 bytes
per log, tail, head, count) and adds each record's length to a cursor at
`0x0800EBA0`. Nothing wraps that cursor at the end of the 0x8000-byte ring,
although the log writer does wrap its own pointers. The shot ring is full, so a
full drain runs off the end after about 200 records (tail 0x695C on 2026-11-08).

Past the end, `0x0801C0DC` computes a negative first length and copies `N + 0x25`
bytes to `buffer - N`, overwriting RAM below `0x2405B078`; the first object
(120 bytes below) is reached about 5 records past the end. `0x080076C0` also has
a `while(1)` trap for lengths over 0x800. The watchdog (about 1.5 s) is refreshed
from the task that serves these requests, so a hang there should reset the
grinder.

This does not explain a failure on the first page, which stays far from the end
of the ring.

## Diagnosis commands

```bash
# Pull FRAM now
coffee-fram-sync-oneshot

# Dry-run backfill with current fram.bin
ssh refuge python3 /volume2/docker/coffee/tools/grinder/backfill.py \
    --fram-bin /tmp/fram.bin --dry-run
```
