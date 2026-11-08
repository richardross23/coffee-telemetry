# Patched FRAM shot-record format (disc distance)

Reference for the `ddd` firmware patch's effect on the FRAM shot log,
for the decoder/backfill tooling. The record layout is unchanged; one
previously-unused field is now populated.

## Record fields
| FRAM bytes | Meaning | Stock | Patched |
|---|---|---|---|
| `rec[4]` | record type (8 = shot) | same | same |
| `rec[5:9]` | timestamp | same | same |
| `rec[9:13]` | target weight, ×0.01 g | same | same |
| `rec[13:17]` | actual weight, ×0.01 g | same | same |
| **`rec[17:21]`** | weight-based shots: unused (0); time-based shots: target time, ms | 0 for weight-based shots | **disc distance, signed int32 LE, µm** |
| `rec[21:25]` | grind time, ms | same | same |

## Reading disc distance
- `mm = struct.unpack("<i", rec[17:21])[0] / 1000`
- `0` = not recorded: pre-patch records, or a shot that ended through a
  path the hook does not cover. Treat as missing.
- Only weight-based (GbW) shots carry it; time-based records keep their
  target time in that field, so gate on record/shot type before reading.
- The value equals the figure shown on the grinder screen, quantised to
  the "DDD Increment" setting. A screen reading of 0.156 mm stores as
  `rec[17:21] = 9c 00 00 00` (156).

## Not affected
- With only this patch, the grinder's `/stats`, `/logs`, and on-screen
  Shot Log ignore `rec[17:21]` for weight-based shots. The optional `logs`
  patch adds it to `/logs`, and `laststat` adds it to `/stats`.
- The Wi-Fi/ESP32 firmware is unmodified.
- `fetch_fram.py` and the sync tooling read the same file, same layout.

## Related
- `DDD-INVESTIGATION.md`: its conclusion that the Wi-Fi module does not
  expose DDD holds for *stock* firmware. With this patch, DDD arrives via
  the FRAM shot record, and over HTTP too with `logs` or `laststat`.
- `BUG-FRAM-STALE.md`: the bytes the decoder treats as a record header
  (`rec[0:4]`) appear to be the firmware's per-record flags (low nibble
  = recipe slot, high nibble = aborted); which record the byte belongs to
  is unconfirmed.
