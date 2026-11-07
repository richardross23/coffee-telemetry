# Mahlkönig E65S GbW — firmware mods (SW300)

Reverse-engineering and custom patches for the grinder's main controller
(STM32H7, image `HEM_NGFW300e.bin`, SW300, built Nov 2021). All three
patches are flashed and confirmed working.

## Files
| File | What it is |
|---|---|
| `HEM_NGFW300e.original.bin` | Stock SW300 main-controller image. **Rollback target.** md5 `fe665b4ecb7d57db091730eb2193f85b` (gitignored) |
| `HEM_NGFW300e.all.bin` | Patched (all three mods below). md5 `8c95871295a657f04c389b39e08dea8e` (gitignored) |
| `HEM-NGWIFI300e.bin` | Stock Wi-Fi/web-interface image (ESP32). Unmodified. md5 `8fe45940ebacf7e83e522a0d0408eafd` (gitignored) |
| `patch.py` | Rebuilds the patched image from the original: `python3 -I patch.py HEM_NGFW300e.original.bin out.bin` |
| `REVERSE-ENGINEERING.md` | Full notes: RAM/flash addresses, every patch byte, how it was found. |
| `FLASH-RUNDOWN.md` | Step-by-step for flashing over the grinder's Wi-Fi updater. |
| `GRINDER-FIRMWARE-PATCH.md` | FRAM shot-record format reference for the log decoder. |

The `.bin` images are gitignored (rebuild them with `patch.py`).

## The three patches
1. **actualdose** — end-of-shot screen shows the measured weight, not the recipe target.
2. **ddd** — each weight-based shot record stores the disc distance in FRAM `rec[17:21]` (int32 LE, µm; ÷1000 = mm; 0 = not recorded).
3. **shotlog** — the Shot Log is reachable without owner mode.

## Flashing
Via the grinder web updater at `http://192.168.4.1` (owner mode → Wi-Fi AP).
See `FLASH-RUNDOWN.md`. Reversible: reflash `HEM_NGFW300e.original.bin`.

## Open follow-up
Wire the disc-distance field (`rec[17:21]`) into `../backfill.py` so it flows
into the dashboard — see `GRINDER-FIRMWARE-PATCH.md`.
