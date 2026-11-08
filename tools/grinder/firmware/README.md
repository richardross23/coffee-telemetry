# Mahlkönig E65S GbW — firmware mods (SW300)

Reverse-engineering and custom patches for the grinder's main controller
(STM32H7, image `HEM_NGFW300e.bin`, SW300, built Nov 2021). All three
patches are flashed and confirmed working on one E65S GbW.

> **Unofficial.** Not made, endorsed or supported by Mahlkönig/Hemro.
> Flashing modified firmware may void your warranty, and a failed flash
> can leave the grinder needing a service visit. Tested on one E65S GbW
> on SW300 only; the same SW300 package covers the E80 / E80 GbW, but
> nobody has tried these patches on one. Use at your own risk.

## The three patches
1. **actualdose** — end-of-grind screen shows the measured weight. Stock
   firmware shows the recipe target there, so a 19.5 g grind against a
   20.0 g target reads "20.0".
2. **ddd** — each weight-based shot record stores the disc distance in FRAM
   `rec[17:21]` (int32 LE, µm; ÷1000 = mm; 0 = not recorded).
3. **shotlog** — the Shot Log is reachable without owner mode.

Together they change 36 bytes of the image.

## Files
| File | What it is |
|---|---|
| `patch.py` | Builds the patched image from the stock one. Checks the input's hash and every original byte before writing. |
| `REVERSE-ENGINEERING.md` | Full notes: RAM/flash addresses, every patch byte, how it was found. |
| `FLASH-RUNDOWN.md` | Flashing (and rolling back) over the grinder's Wi-Fi updater. |
| `GRINDER-FIRMWARE-PATCH.md` | FRAM shot-record format reference for the log decoder. |

No firmware images are in this repo: you supply Mahlkönig's stock image
and build the patched one locally. `*.bin` here is gitignored.

## Getting the stock image
`HEM_NGFW300e.bin` is in Mahlkönig's SW300 update package
(`01_Files if Webserver is available (after SW300)/`), a copy of which is
posted on home-barista.com. Check it before using it:

| | md5 | sha256 |
|---|---|---|
| stock `HEM_NGFW300e.bin` | `fe665b4ecb7d57db091730eb2193f85b` | `bdb66ad30c4db40ab3d0db3034b8693aed878bf47b934370f89a8a6a8ed4882b` |
| patched (all three) | `8c95871295a657f04c389b39e08dea8e` | `a15c5d0e143c40daa842a4a00868823eea19e62e234ebb141b9fb2eff571e772` |

The stock md5 is the one Mahlkönig's own SW300 how-to PDF shows in its
update screenshot. Keep the stock file: it is your rollback image.

## Building
```
python3 -I patch.py HEM_NGFW300e.bin HEM_NGFW300e.all.bin
```
It prints the output's md5 and sha256, which should match the table. To
build a subset: `--patches actualdose,shotlog`.

## Flashing
Over the grinder's Wi-Fi updater at `http://192.168.4.1` (owner mode →
Wi-Fi access point). See `FLASH-RUNDOWN.md`. Reversible: flash the stock
image the same way.

The disc distance lands in the FRAM shot log; `../backfill.py` reads it
from a FRAM dump into each shot's `grinder_setting`.
