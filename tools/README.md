# tools/

Scripts that aren't part of the running stack but are useful around it.

## `grinder/` — Mahlkönig E65S GBW

The grinder has a diagnostic WiFi AP that serves its logs over
unauthenticated HTTP at `http://192.168.4.1`. On stock firmware it only
comes up in owner mode (hold both buttons while powering on); the optional
`wifiboot` firmware patch brings it up at every power-on. These tools get
the grinder's measurements into the espresso shot archive, two ways:

- **Live.** On patched firmware, `grinder_poll.py` reads each grind from
  `/stats` and `shot-logger` attaches it as the shot saves. See
  [Live sync](#live-sync).
- **FRAM sync.** On any firmware, pull the grinder's whole log and pair
  shots with grinds afterwards. See [FRAM sync](#fram-sync).

The firmware patches are in [`grinder/firmware/`](grinder/firmware/README.md).

| Script | What it does |
|---|---|
| `grinder_poll.py` | **Live sync.** Runs on a Mac through the morning, publishes each new grind over MQTT, and reports the result at the end. Tests: `python3 grinder/test_grinder_poll.py`. |
| `com.rwross.coffee-grinder-poll.plist` | launchd job that starts `grinder_poll.py` at 06:30 and at login. |
| `coffee-fram-sync-oneshot` | **One-touch command.** Joins the grinder AP, invokes `coffee-fram-sync`, restores prior WiFi. Designed to run inside the grinder's ~2-minute no-client AP timeout. Bind to a macOS Shortcut or iOS SSH shortcut for literal one-tap. |
| `coffee-fram-sync` | **Lower-level command.** Shell wrapper run from a Mac that's on the grinder's AP (WiFi) and refuge (ethernet). Pulls `fram.bin`, scps to refuge, invokes `backfill.py`. |
| `backfill.py` | Parses a `fram.bin`, pairs each shot with the grind made just before it (after correcting for the grinder clock's offset), writes `ground_weight_g` + `grind_duration_s` into shot metadata (plus `grinder_setting` from the measured disc distance, on patched firmware), rebuilds the dashboard index. Idempotent. Tests: `python3 grinder/test_backfill.py`. |
| `fetch_fram.py` | Standalone FRAM downloader (POST /stm?framFetch → poll /status → GET /fram.bin). Used by `coffee-fram-sync` conceptually; usable directly if you want just the file. |
| `decode_fram.py` | Converts a `fram.bin` to the text log format `import_grinder_log.py` expects. Legacy — `backfill.py` reads FRAM directly. |
| `import_grinder_log.py` | Legacy pipeline: parses a text log paste from the grinder's `/logs?Shots` endpoint and publishes updates via MQTT. Kept for anyone preferring that flow. |
| `probe_grinder.py` | Walks the grinder's HTTP endpoints and prints what each returns. |

### Live sync

Needs the `laststat` firmware patch (and `ddd` for disc distance), with
the grinder's WiFi on all morning, which in practice means `wifiboot`. Also
needs a Mac with Ethernet for the LAN and WiFi joined to the grinder.

1. `grinder_poll.py` reads `/stats` every 10 seconds from 06:30 to 11:05
   and publishes each new grind, retained, to `coffee/grinder/last`.
2. When a shot ends, `shot-logger` takes the newest unused grind the Mac saw
   before the pump started, up to 15 minutes back, and writes the dose, grind
   time and disc distance into the shot. A grind usually comes 1-2 minutes
   before its shot. One made while a shot is running waits for the next shot.
3. At 11:05 the job counts the morning's shots and runs `coffee-fram-sync` if
   any missed its grind. `backfill.py` leaves live-matched shots alone. Then
   it publishes a report to `coffee/grinder/report`.

To install, edit the constants at the top of `grinder_poll.py` and the paths
in the plist, then:

```sh
install -m 755 tools/grinder/grinder_poll.py ~/.local/bin/coffee-grinder-poll
cp tools/grinder/com.rwross.coffee-grinder-poll.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.rwross.coffee-grinder-poll.plist
```

The log is `~/Library/Logs/coffee-grinder-poll.log`.

The report reaches Home Assistant through MQTT discovery as
`sensor.coffee_grinder_report`. Its state is the date, and its `message`
attribute is the text to push, empty when there's nothing to say. This
automation sends it, and warns if the Mac sent nothing:

```yaml
- id: coffee_grinder_daily_report
  alias: 'Coffee Grinder: daily report'
  trigger:
  - platform: time
    at: '11:15:00'
  action:
  - choose:
    - conditions:
      - condition: template
        value_template: "{{ states('sensor.coffee_grinder_report') != now().strftime('%Y-%m-%d') }}"
      sequence:
      - service: notify.mobile_app_your_phone
        data:
          title: Coffee Grinder
          message: No report from the Mac today.
    - conditions:
      - condition: template
        value_template: "{{ state_attr('sensor.coffee_grinder_report', 'message') | default('', true) != '' }}"
      sequence:
      - service: notify.mobile_app_your_phone
        data:
          title: Coffee Grinder
          message: "{{ state_attr('sensor.coffee_grinder_report', 'message') }}"
```

### FRAM sync

1. Make sure the grinder's WiFi is on: hold both buttons during power-on
   on stock firmware, or any power-on with `wifiboot`.
2. Trigger the sync: either `coffee-fram-sync-oneshot` from a terminal, a
   macOS Shortcut you've bound to a menu-bar icon or hotkey, or an iOS
   Shortcut that runs it over SSH. The whole cycle takes ~70s. On stock
   firmware it must complete before the grinder's ~2-minute no-client AP
   timeout — the oneshot script joins the AP first so the clock starts then.
3. Reload the dashboard. Row chips will show e.g. `19.78g ground`
   / `5.38s grind` instead of the plain dose label.


### Grinder quirks the tooling absorbs

- The RTC doesn't switch for DST on its own and drifts a few minutes per
  month. `backfill.py` estimates the clock's offset per day from the
  shots themselves, so set the grinder to whatever time is convenient,
  whenever you like; DST changes, drift and resets don't affect
  matching. It prints a per-month raw-gap report so the offset is
  visible.
- A grind logged as 0 g is dropped, and its shot stays unmatched rather
  than taking the next grind.
- On stock firmware, normal power-off kills the WiFi AP and the owner-mode
  button-hold must be repeated on each power-on. `wifiboot` removes that,
  at the cost of leaving the AP on with its published default password.
- `/stats` is cut off at about 1 KB, so it isn't valid JSON. `grinder_poll.py`
  reads it with a regex. The last-shot fields read 0 until the first grind
  after each power-up.
- The AP is single-radio and the ESP32 web server slows under load;
  one request at a time, don't hammer it.