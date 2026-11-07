# Flashing a patched image

Procedure for flashing a patched main-controller image to the Mahlkönig
E65S GbW over its Wi-Fi web updater. This REPLACES the main-controller
firmware. It is reversible (reflash the original) as long as the
grinder's Wi-Fi comes back up, but a mis-step can require a factory
reflash, so follow the gates and stop on any anomaly.

## Images (in this folder)
- `HEM_NGFW300e.all.bin` — patched (all three mods), md5
  `8c95871295a657f04c389b39e08dea8e`.
- `HEM_NGFW300e.original.bin` — stock, for rollback, md5
  `fe665b4ecb7d57db091730eb2193f85b`.

What the patched image changes: end-of-shot screen shows actual dose;
weight-based shot records store disc distance in FRAM `rec[17:21]`; the
Shot Log is visible without owner mode. See `GRINDER-FIRMWARE-PATCH.md`
and `REVERSE-ENGINEERING.md`.

## Prerequisites
1. Grinder in owner mode (hold both hard keys during power-on).
2. On the grinder: Service → WiFi Access Point.
3. The machine doing the flash joined to the grinder's `E65…` AP
   (password `e65sgbwdemo`, Mahlkönig's factory default). If that machine
   also has a wired uplink, it stays reachable while Wi-Fi talks to the
   grinder.
4. Confirm reachability: `curl -s http://192.168.4.1/info`.

## Flash sequence
Derive the exact upload → verify → flash calls from the grinder's own web
UI, which is authoritative — don't guess:
- `curl -s http://192.168.4.1/app.js` (and `chunk-vendors.js`) and read
  the UpdateFirmwareService / VerifyFirmwareService logic, or reuse the
  repo's grinder HTTP tooling (`fetch_fram.py`, `probe_grinder.py`) as the
  pattern.
- Sequence: upload the `.bin` → commit its MD5 and request verify
  (returns an "imageVerified" result) → only then request flash
  (`/stm?flash`) → poll `/status` (framProgress / stm state) to completion
  → the grinder reboots itself.

## Hard gates
- Confirm the local md5 of `HEM_NGFW300e.all.bin` is
  `8c95871295a657f04c389b39e08dea8e` before uploading.
- The grinder must report the image VERIFIED before the flash command.
  If verify does not clearly succeed, STOP — do not flash.
- One request at a time (single-radio AP, slow web server).
- During flash/reboot: do not interrupt, do not cut power, do not drop
  the Wi-Fi.
- If anything is ambiguous or an endpoint errors, STOP rather than
  retrying blind.

## Rollback
Same procedure with `HEM_NGFW300e.original.bin` (md5
`fe665b4ecb7d57db091730eb2193f85b`).

## After flashing
- Power-cycle normally (no button hold) and check the Logs menu: "Shot
  Log" should appear and open without owner mode.
- Actual-dose-on-screen and the disc-distance FRAM field show up on the
  next real shot.
