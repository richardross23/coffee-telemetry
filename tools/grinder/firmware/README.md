# Mahlkönig E65S GbW — firmware mods (SW300)

Reverse-engineering and custom patches for the grinder's main controller
(STM32H7, image `HEM_NGFW300e.bin`, SW300, built Nov 2021). The three
core patches are flashed and confirmed working on one E65S GbW. Three
optional ones run on the same grinder, with much less time behind them.

> ## ⚠️ Read this first
>
> **I'm a hobbyist tinkerer, not a firmware engineer.** This was
> vibe-coded: an AI assistant (Claude) did the reverse engineering and
> wrote the patches, and I flashed the result onto my own grinder. It has
> been tested on exactly one machine: my E65S GbW on SW300.
>
> - **You can brick your grinder.** Flashing modified firmware can leave
>   it dead, misbehaving, or needing a service visit, and will probably
>   void your warranty.
> - **I take no responsibility** for anything that happens to your
>   grinder, your coffee, or you as a result of using any of this.
> - **There is no support.** If it goes wrong, nobody (me included) is
>   obliged to help you fix it.
> - **Unofficial.** Not made, endorsed or supported by Mahlkönig/Hemro.
> - **Untested elsewhere.** The E80 / E80 GbW share the SW300 update
>   package, but nobody has tried these patches on one.
> - If you don't understand what each flash step does, don't do it.
>
> Provided as is, with no warranty of any kind (see the repo's MIT
> `LICENSE`). You use it entirely at your own risk.

## The three patches
1. **actualdose** — end-of-grind screen shows the measured weight. Stock
   firmware shows the recipe target there, so a 19.5 g grind against a
   20.0 g target reads "20.0".
2. **ddd** — each weight-based shot record stores the disc distance in FRAM
   `rec[17:21]` (int32 LE, µm; ÷1000 = mm; 0 = not recorded).
3. **shotlog** — the Shot Log is reachable without owner mode.

Together they change 36 bytes of the image.

## Optional patches
Only built if you ask for them with `--patches`. Flashed on one grinder on
2026-11-09.

4. **logs** — fixes the web log, which stops partway through on stock
   firmware. Shots are read newest first, with 0.01 g weights and the disc
   distance (needs `ddd`). The newest 12 shots take two requests.
5. **laststat** — `/stats` carries the last shot, replacing the counters for
   recipes 4–6. The fields read 0 until the first grind after power-up, and
   their values have only been checked in an emulator so far.
6. **wifiboot** — Wi-Fi starts at every power-up, with no key hold. Holding
   both keys still logs in as owner.

> **`wifiboot` leaves the access point on all the time with Mahlkönig's
> published default password.** Anyone in range can reach the grinder's web
> page and its firmware updater. Whether having Wi-Fi on affects weighing
> hasn't been tested.

## Files
| File | What it is |
|---|---|
| `patch.py` | Builds the patched image from the stock one. Checks the input's hash and every original byte before writing. |
| `REVERSE-ENGINEERING.md` | Full notes: RAM/flash addresses, every patch byte, how it was found. |
| `FLASH-RUNDOWN.md` | Flashing (and rolling back) over the grinder's Wi-Fi updater. |
| `GRINDER-FIRMWARE-PATCH.md` | FRAM shot-record format reference for the log decoder. |
| `emu_grinder.py`, `test_patches.py` | Run the firmware's log and `/stats` code in an emulator, to test patches before flashing. Needs `pip install unicorn`. |

No firmware images are in this repo: you supply Mahlkönig's stock image
and build the patched one locally. `*.bin` here is gitignored.

## Getting the stock image
`HEM_NGFW300e.bin` is in Mahlkönig's SW300 update package
(`01_Files if Webserver is available (after SW300)/`). A copy is linked
in [this r/espresso comment](https://www.reddit.com/r/espresso/comments/1063z0w/comment/j5wlc6v/).
Check it before using it:

| | md5 | sha256 |
|---|---|---|
| stock `HEM_NGFW300e.bin` | `fe665b4ecb7d57db091730eb2193f85b` | `bdb66ad30c4db40ab3d0db3034b8693aed878bf47b934370f89a8a6a8ed4882b` |
| patched (all three) | `8c95871295a657f04c389b39e08dea8e` | `a15c5d0e143c40daa842a4a00868823eea19e62e234ebb141b9fb2eff571e772` |
| patched (all six) | `ff37c39df3735291802b65ce20a25001` | `162413d74f3767e95d1181a1780e7817aa35f8b0ae907bb3e4809f65e0b03d2c` |

The stock md5 is the one Mahlkönig's own SW300 how-to PDF shows in its
update screenshot. Keep the stock file: it is your rollback image.

## Building
```
python3 -I patch.py HEM_NGFW300e.bin HEM_NGFW300e.all.bin
```
It prints the output's md5 and sha256, which should match the table. To
build a subset: `--patches actualdose,shotlog`. All six:
`--patches actualdose,ddd,shotlog,logs,laststat,wifiboot`.

## Flashing
Over the grinder's Wi-Fi updater at `http://192.168.4.1` (owner mode →
Wi-Fi access point). See `FLASH-RUNDOWN.md`. Reversible: flash the stock
image the same way.

The disc distance lands in the FRAM shot log; `../backfill.py` reads it
from a FRAM dump into each shot's `grinder_setting`.
