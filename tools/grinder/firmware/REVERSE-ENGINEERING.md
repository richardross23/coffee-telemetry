# Mahlkönig E65S GbW / E80 — SW300 firmware reverse-engineering

Notes from reverse-engineering the grinder's main-controller firmware
(`HEM_NGFW300e.bin`, SW300, built Nov 2021) and building six patches.
All addresses are in the running image. Patches 1-3 are confirmed working
on hardware; "Patches 4-6" below says how far the others have been tested.

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
level > login. Login levels: 0 = none, 1 = owner, 5 = service.

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
- `laststat`: the fields appear in `/stats`. They read 0 from power-up until the
  first grind finishes, because the record is in RAM, so their values have only
  been checked in the emulator.

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
4-6 are described above. Build them with `patch.py` from the stock image.
