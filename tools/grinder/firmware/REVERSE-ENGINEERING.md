# Mahlkönig E65S GbW / E80 — SW300 firmware reverse-engineering

Notes from reverse-engineering the grinder's main-controller firmware
(`HEM_NGFW300e.bin`, SW300, built Nov 2021) and building eight patches.
All addresses are in the running image. Patches 1-3 are confirmed working
on hardware; "Patches 4-6" and "Patches 7-8" below say how far the others
have been tested.

## Image
- STM32H7 + TouchGFX, raw image based at `0x08000000`, built `-O0`.
- Trailer: `0x012C` (version 300), `0xCAFECAFE`, build date string.
- No image checksum in the trailer, and the STM32 does no flash
  self-check at boot (its CRC unit is used only for UART packets, plus
  TouchGFX's chip-ID check).

## Key RAM structures
- Machine state at `0x24058F80`: `+0x40` target weight, `+0x90` measured
  weight (both u32, 0.01 g units), `+0x88` final-weight-captured flag,
  `+0xAC` live disc distance (DDD), signed, in µm.
- Recipe table at `0x24059558`, 16 bytes each: flags, `+4` weight,
  `+8` target DDD.
- Settings at `0x240593A0`: `+0x76` login timeout, `+0xED` "Weight Dec.
  Digits" (range 0–2).

## Patch 1 — actual dose on screen
Stock firmware reloads the recipe *target* onto the end-of-shot screen
(and caps the live reading at target while grinding). At `0x0804D390`
the display routine loads `+0x40` (target) into the display variable
`0x24055C6C`. Repoint it to `+0x90` (measured):

```
file off 0x4D392 (12 bytes)
before: 1a6c 1346 9b00 1344 5b00 1a46   ldr r2,[r3,#0x40]; r2 *= 10
after:  d3f8 9020 02eb 8202 5200 00bf   ldr.w r2,[r3,#0x90]; r2 *= 10; nop
```

The displayed decimals follow the "Weight Dec. Digits" setting; weights
are stored in 0.01 g units, so two decimals is the true resolution.

## Patch 2 — disc distance per shot (DDD)
The shot record has a 4-byte field (`rec[17:21]`, image offset `+0xD`
within the record struct at `0x2405AAA4`) that is unused/zero for
weight-based shots (it holds target time only for time-based shots).
A hook copies the live DDD into it when a weight-based shot's record is
appended.

- At the log-append call site `0x0800B1B8`, change `bl 0x0801C03C`
  (`10f040ff`) to `bl 0x08183760` (`78f1d2fa`).
- Stub in the zero padding after the `.data` load image
  (`0x0818374C`–`0x081DFE00`, unreferenced; no MPU region covers flash),
  bytes `034bb3f9ac30c0f80d3098f667bc00bf808f0524`:
  ```
  ldr   r3,=0x24058F80
  ldrsh r3,[r3,#0xAC]     ; live DDD, µm
  str.w r3,[r0,#0xD]      ; shot record spare field
  b.w   0x0801C03C        ; continue into the normal log-append
  ```

Readers of `+0xD` (`0x0801D524/568`, `0x08047088/0B4`) are all on the
time-based branch, so the hook does not disturb them. Decode the stored
value as int32 LE ÷ 1000 = mm. See `GRINDER-FIRMWARE-PATCH.md` for the
full FRAM record format.

## Patch 3 — Shot Log without owner mode
Menu visibility: the filter at `0x080444F4` reads each row's required
level from the layout table `0x240508BC` (menu stride 0x32 slots, slot
stride 0x10; slot+0 = item id, slot+4 = required level) and compares it
to the current login level at `state+0xF5`; it hides the row if
level > login. Login levels: 0 = none, 1 = owner, 2 = service, 3 =
manufacturer. Level 5 is never assigned, so a row at 5 is hidden from every
login; several rows use it when option bit 0 of `0x081E0014` is clear.

The Machine Logs list is menu 5. Its rows (ids `0x1D` Error, `0x1E`
Shot, `0x1F` Alerts, `0x20` Service) are all placed at level 1 (owner),
so a logged-out user (level 0) sees none of them. The row dispatcher at
`0x08044848` maps row id → selector (`0x2405473C`) + the detail menu
(11); id `0x1E` → selector 0 → Shot Log.

Two bytes make Shot Log alone visible to everyone:

```
0x0804B378  01 23 -> 00 23   list row id 0x1E (menu 5): level 1 -> 0  (the gate)
0x08047348  05 23 -> 00 23   detail-view header id 0x71 (menu 11): gated level -> 0
```

Error/Service/Alerts logs are left owner-gated.

## How a weight-based grind works
Read from the stock image and not exercised on hardware. The ADC sample rate is
unknown, so limits that count samples aren't converted to time. `S` is the machine
state at `0x24058F80`, `SET` the settings at `0x240593A0`.

**Weighing** (ADC callback `0x080039F0`). The raw load-cell value passes through
moving averages of 1111, 523 and 359 samples. Grams = (x − cal0) × 1000 / (cal1 −
cal0), with cal0 and cal1 at `0x2400002C` and `0x24000030`. A separate noise figure
comes from the last 1000 samples, averaged over 300; `S+0xDC` holds it in 0.01 g.
The scale counts as quiet when that is under the limit chosen by GbW Resolution
(`SET+0x65`): 0 = Low `SET+0x8C` (default 50), 1 = Medium `SET+0x8A` (20), otherwise
High `SET+0x88` (5), in 0.01 g. The factory value is 0.

**States** (`S+0`, jump tables from `0x08009EEC`):

| State | Meaning |
|---|---|
| 0x11 | idle |
| 0x21 | portafilter seen, waiting for quiet |
| 0x22-0x25 | portafilter matching and capture |
| 0x42 | recipe chosen, waiting to start |
| 0x82 / 0x83 | weight-based / time-based grind |
| 0x84 / 0x85 | manual grind |
| 0x101 | weight-based shot finished |
| 0x102 / 0x103 | time-based shot finished |
| 0x1002 / 0x1003 | paused |
| 0x401-0x40F | load-cell calibration |

**Before the grind.** With portafilter matching on, the weight is compared with the
saved table at `0x24059350` (8 bytes each: `+1` recipe, `+2` tolerance, `+4` weight;
kept at FRAM 0xB00). At 0x42, `S+0x40` takes the recipe target and `S+0x44` the
recipe's correction ÷ 1000. Once quiet, `0x08003948` tares (`S+0x70`) and the state
goes to 0x82.

**Stopping.** In 0x82, every sample updates running sums for a least-squares line
through the net weight since tare. After `SET+0xA8` samples (default 1000), `S+0x74`
holds the line's current value. A counter (`S+0x7C`) rises while both the line and
the net weight exceed target + correction, and at `SET+0xAA` counts (default 2000)
the state goes to 0x101 and the motor stops.

**Learning** (`0x0800AD52`). Runs once per weight-based shot, on the first quiet
reading within 5000 ms of stopping. With e = target − actual in 0.01 g, and
2 ≤ |e| < 600, the recipe's correction (`+0xC`, in 1/1000 of 0.01 g) changes by
e × 1000 / N. N is Const. Overshot Comp. (`SET+0xF4`, default 4). If that is Off,
N = |correction / (e × 1000)|, minimum 1. A correction of 0 takes the whole error.
Nothing is learned after a pause or a manual top-up. `0x080079D8` saves the recipe
table to FRAM 0x700.

The shot log's weight is a different reading: `0x0800AFC6` rewrites it on every
quiet pass until the portafilter is lifted, so it is the last quiet reading rather
than the one learning used.

**What is never learned.** Tare handles zero for each grind. Scale gain only changes
through calibration, which averages `SET+0xC4` (default 500) readings, while the
noise is under `SET+0xC0` (500), into `SET+0xB8` (zero) and `SET+0xBC` (span). Dose
Timeout (`SET+0x44`) only applies to pauses.

## Logs in FRAM
Four rings; bases at `0x24000468`, sizes at `0x24000478`, control blocks loaded
from FRAM 0x1000 (12 bytes each: tail, head, count).

| Ring | Base | Size | Holds |
|---|---|---|---|
| 0 | 0x01080 | 0x08000 | shots, 29-byte records |
| 1 | 0x09080 | 0x08000 | load-cell events |
| 2 | 0x11080 | 0x04000 | errors, calibration, login timeouts |
| 3 | 0x15080 | 0x29000 | power, settings; each boot writes Grinder Off, then On |

A record is a type byte, a timestamp, then a payload whose size comes from the
table at `0x24000488`. The on-screen reader wraps its cursor; the `/logs` reader
doesn't (see `../BUG-FRAM-STALE.md`). Event names are at `0x08082934`, 0xBC bytes each.

## Link to the Wi-Fi module
USART1 at 115200: `0x32`, length, command, payload, CRC-16. Log requests are
command `0x0A`, sub-command `0x0C`, then log type and reset flag. `/stats` is
sub-command `0x0B`; its JSON is built on the main controller at `0x0800E014`. FRAM
pulls are one request per 1 KB page.

## Power to the Wi-Fi module
The main controller switches the module on and off. `0x24050618` is the Wi-Fi flag,
in RAM: set at boot if both keys are held (`0x0800A2E8`) and toggled by the service
menu (`0x0804B88C`). `0x08007F94` runs every 50 ms and ramps the module's supply
through DAC1 on PA4, then raises PA8 and PA10; init drives both low (`0x08007D40`,
`0x08007DFE`). So when the main firmware restarts, the Wi-Fi goes off unless the
flag is set again. Login timeouts don't touch the flag.

## Patches 4-6: logs, laststat, wifiboot
Optional, in `patch.py`. `test_patches.py` runs them in an emulator, which covers
the code's logic but not timing or peripherals. All three were flashed on one
grinder on 2026-11-09.

- `logs`: a full drain of `/logs?Shots` returned all 1,129 rows in 24 s, newest
  first, with every timestamp matching a FRAM dump and no restart. It crossed the
  end of the ring, where the stock reader runs off.
- `wifiboot`: the access point came back after the flash with no keys held.
- `laststat`: after a real grind, the time, target, actual, disc distance and grind
  time in `/stats` matched the newest `/logs` row. The fields read 0 from power-up
  until the first grind finishes, because the record is in RAM.

## Patches 7-8: enddecimals, finalweight
- `enddecimals`: on the dose screen, the BL at `0x0804D59A` returns 2 decimals when
  the state is 0x101 and the "Weight Dec. Digits" setting otherwise. Flashed on
  2026-11-09 and seen on the grinder's screen on 2026-11-10.
- `finalweight`: the logged weight (`0x2405AAA4+9`) is rewritten on every steady
  pass of state 0x101 while the net weight is above 0, and the record is appended
  once the portafilter is lifted and the state is more than `SET+0x48` ms old
  (default 1000). The screen closes at that point, so the logged number can't be
  shown first. The patch stops the rewrite at `0x0800AFD4` once `S+0x54` (ms in
  this state, reset on every state change) reaches 2000 and a weight is recorded,
  and the screen load at `0x0804D392` reads the record, falling back to the live
  weight while it is 0 (`0x0800A88C` clears it before each grind). Dosing and
  learning are untouched. Tested in the emulator, through the real state machine;
  never flashed. A locked reading is no more accurate than the last steady one, so
  this grinder keeps the live number.

## Flashing and recovery
The Wi-Fi/web module flashes the STM32 through ST's ROM UART bootloader
(init bootloader, remove write protection, erase, write, reboot). The
only integrity check is an MD5 of the uploaded file against a value the
client sends — a transport check, no signature. Device settings live in
flash sector `0x081E0000`, untouched by the image. Because the ROM
bootloader cannot be erased, a bad image is reflashable as long as the
Wi-Fi access point comes up; the last-resort recovery is SWD with the
board opened. See `FLASH-RUNDOWN.md`.

The Wi-Fi firmware starts the bootloader itself (`stm32DFU`, `0x400DA840`): it
pulses ESP32 GPIO27 and raises GPIO26, which look like the STM32's NRST and BOOT0,
then sends the 0x7F sync at even parity. The main image plays no part, so a bad
image can't block a reflash while the module runs, and the module stays up while
the STM32 is in ROM mode. The access point falls back to the SSID `Wave2_Fallback`
plus the MAC, same password, when a flag at `0x3FFC2104` is 0, probably when the
main controller hasn't sent its details. Untested: whether the module starts from
a cold boot when the main firmware is dead, which decides whether a failed flash
can be recovered over Wi-Fi.

## Status
Patches 1-3 flashed and confirmed on hardware: actual dose on screen, disc
distance in the FRAM record, Shot Log reachable without owner mode. Patches
4-8 are described above. Build them with `patch.py` from the stock image.
