# Flashing a patched image

How the patched main-controller image was flashed to an E65S GbW over its
Wi-Fi updater. This replaces the main-controller firmware. It is
reversible (flash the stock image the same way) as long as the grinder's
Wi-Fi comes back up, but a mis-step can need a service reflash, so follow
the gates and stop on anything unexpected. See the disclaimer in
`README.md`.

The grinder must already run SW300 (the version with the web page at
`192.168.4.1`). Older firmware needs Mahlkönig's own update first.

## Before you start
1. Build the image with `patch.py` and check its md5 against the table
   in `README.md`.
2. Power on in owner mode: hold both front keys while switching on.
   Mahlkönig's SW300 notes say this logs in as owner with Wi-Fi enabled.
   With `wifiboot` already flashed, a normal power-on is enough.
3. Join the grinder's `E65…` Wi-Fi network. Password `e65sgbwdemo`, the
   default printed in Mahlkönig's SW300 update instructions.
4. Check it answers: `curl -s http://192.168.4.1/info`.

## Flash
These are the calls the grinder's own web page makes (read from its
`app.js`); they were run with curl from a Mac. Mahlkönig's notes say the
page's "Software update" area does the same from a browser, but the
patched image has only been flashed with the commands below.

```
FW=HEM_NGFW300e.all.bin

# 1. upload (~2 MB; takes a while over the grinder's Wi-Fi)
curl -sS -X POST --data-binary "@$FW" -H 'Content-Type: application/octet-stream' http://192.168.4.1/upload

# 2. ask the grinder to check the upload against its md5
curl -sS -X POST --data "$(md5 -q "$FW")" 'http://192.168.4.1/stm?verify'   # Linux: md5sum "$FW" | cut -d' ' -f1
curl -sS http://192.168.4.1/status    # must now say "imageVerified": true

# 3. only then flash, and poll until "updateProgress" reaches 100
curl -sS -X POST 'http://192.168.4.1/stm?flash'
curl -sS http://192.168.4.1/status    # repeat; "flashFailed" must stay 0
```

The grinder reboots itself when the flash finishes. The flash takes about
4 minutes, and it also worked after the owner login had timed out.

## Hard gates
- Don't flash unless `/status` shows `"imageVerified": true` after step 2.
  The md5 check is the updater's only integrity check.
- One request at a time: the access point is single-radio and its web
  server is slow.
- During the flash and reboot: don't cut power and don't drop the Wi-Fi.
- If an endpoint errors or anything looks off, stop rather than retrying
  blind.

## Rollback
The same three steps with the stock `HEM_NGFW300e.bin` (md5
`fe665b4ecb7d57db091730eb2193f85b`).

## After flashing
- Switch on normally (no key hold) and open the Logs menu: "Shot Log"
  should appear and open without owner mode.
- The next grind's end screen shows the measured weight, and its FRAM shot
  record carries the disc distance.
- With `wifiboot`, the access point returns about a minute after the flash
  with no keys held.
