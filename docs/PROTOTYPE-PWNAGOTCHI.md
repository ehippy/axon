# Fast-track prototype: repurposed pwnagotchi parts on a Pi Zero 2 W

A dormant pwnagotchi on the desk has a battery (PiSugar) and a display (Waveshare
2.13" e-Paper) already wired up. Plan: move that HAT + PiSugar off the pwnagotchi's
original Pi Zero W and onto the spare **Pi Zero 2 W** instead — same Zero form
factor (65×30mm, same GPIO header position/mounting holes), so the HAT and PiSugar
should drop right on once a 40-pin header is soldered onto the Zero 2 W (it ships
headerless). Gets a physical prototype running this week instead of waiting on the
Pi4+Feather build (see [SPEC.md](SPEC.md)). No SD card yet — needs a fresh image.

## What's actually in it

| Part | Confirmed | Notes |
|---|---|---|
| SBC | Raspberry Pi Zero 2 W Rev 1.0, confirmed via `/proc/cpuinfo` | Hostname `display-device` (`device.example`), `pi` user, SSH key access set up |
| OS | Debian 13 "trixie", 64-bit (`aarch64`) | Raspberry Pi OS default for Zero 2 W. Earlier note in this doc suggesting 32-bit to save memory was likely backwards — Claude Code's CLI and its native Node deps increasingly ship arm64-only prebuilt binaries (same trend that dropped ARMv6), so 64-bit may be required, not just heavier. Good that it's already set up this way. |
| Display | Waveshare 2.13" e-Paper HAT (~250×122 monochrome, SPI) | Moved over from the pwnagotchi. Driver confirmed: **V3** (`epd2in13_V3` from the [waveshareteam/e-Paper](https://github.com/waveshareteam/e-Paper) repo) — V2 ran with no errors but no visible change (classic driver-mismatch symptom: wrong init sequence, panel silently ignores it); V3's demo produced real busy/refresh cycling and a visible update. |
| Battery | **PiSugar3**, confirmed by hardware inspection | Shows up on I2C bus 1 at `0x57` (plus `0x68`, likely its onboard RTC) — note this contradicts the address table this doc had guessed from (PiSugar2 ≈ 0x57, PiSugar3 ≈ 0x2d); trust the hardware ID over that guess. Battery % not wired into software yet. |
| GPIO header | Soldered, HAT + PiSugar seated | Both physically installed |

**Measured at idle right after first boot** (`free -h`): 415Mi total RAM (of the
512MB physical — rest reserved for GPU/firmware), 134Mi already used by the base OS
+ sshd, 280Mi available, and a 414Mi swap already 20Mi into use doing essentially
nothing. That's the real headroom Claude Code's Node process has to fit into —
worth watching closely once it's installed, not just a theoretical concern anymore.

## Why this beats the original Zero W plan

The original Pi Zero W is ARMv6 (ARM1176). Node.js officially dropped ARMv6 support
after v11 (2018) — unofficial community builds exist but are unsupported and break
across releases, and even a working Node wouldn't make Claude Code's CLI pleasant on
a single-core 1GHz chip. The Zero 2 W's BCM2710A1 is the same ARMv7/ARMv8
Cortex-A53 family as the Pi 3 — Node.js runs on it normally, no workarounds needed.

**Remaining watch-item: still only 512MB RAM.** That's now the only real question
mark, not the CPU architecture. Likely workable for Claude Code + remote networking +
a lightweight Python display service if nothing else heavy runs alongside them, but
worth validating empirically rather than assuming — add a modest swap file as a
safety margin (not a performance layer; avoid relying on it under sustained load,
it'll thrash the SD card) and watch actual memory usage once it's running.

If 512MB turns out too tight in practice, the fallback is the same re-scoped role
from the original plan: a remote networking-connected status display / SSH jump point,
with actual Claude Code execution staying on the Pi4 puck (or elsewhere) and
pushing status to this box over the network.

## Software plan

- **OS**: Done — Debian 13 (trixie), 64-bit, flashed and SSH-reachable at
  `device.example`.
- **Remote access**: remote networking — standard package-manager install works fine now
  (no ARMv6 workaround needed).
- **Agent runtime**: try Claude Code directly on-device first, now that the CPU
  blocker is gone. Fall back to the status-display-only role if RAM pressure makes
  it impractical.
- **Display service**: Python using Waveshare's official e-Paper Python library.
  Whether it's rendering local agent status or relaying pushes from elsewhere
  depends on which role wins out above — same coarse-grained-state design either
  way (e-ink's refresh speed doesn't suit scrolling text or fast animation).
- **PiSugar**: once the model's confirmed, its software (PiSugar-power-manager, a
  small HTTP service PiSugar themselves provide) reports battery level — worth
  wiring into the status display regardless of which role this box ends up playing.

## PiSugar3 software

Installed via the official installer (`wget https://cdn.pisugar.com/release/pisugar-power-manager.sh && sudo bash pisugar-power-manager.sh -c release`):
`pisugar-server` (a systemd service) exposes a web UI on `:8421` and a plain-text
line-based query protocol on `:8423` (no REST/JSON despite some docs implying
otherwise — it's `get battery`, `get battery_charging`, etc. over a raw TCP socket,
newline-terminated). Confirmed working: model reports `PiSugar 3`, battery 100%,
charging, 4.2V. Wrapped in `software/display-service/pisugar_client.py` — the
prototype UI now shows real battery % instead of a placeholder.

### CPU fix: --ws flag was pegging a full core

Noticed load average sitting around 1.0 on a quad-core box (via the
DIAGNOSTICS screen) - traced to `pisugar-server` alone burning 98% CPU
continuously (22m46s of CPU time in 23 minutes of uptime), confirmed with
`ps aux` and `strace -c` (steady `epoll_pwait`/`clock_gettime`/`write` churn,
no errors - a busy loop, not a crash spiral). Isolated by testing flag
combinations directly: dropping `--ws` (the standalone WebSocket push
server) alone fixed it, CPU dropped to ~0.5% and stayed there. `--http`
(the browser dashboard on `:8421`) and `--web` were NOT the cause - kept
both, so the dashboard still works. Only `axon-assets`-relevant interface,
the raw TCP protocol on `:8423`, was never affected either way.

Fixed by editing `/etc/default/pisugar-server`'s `OPTS` to drop `--ws
0.0.0.0:8422` and restarting the service (original backed up alongside it
as `pisugar-server.bak`). Not something this repo tracks (it's a system
config file on display-device, not a repo file) - if display-device ever gets reflashed, this
needs redoing. Worth it: on a battery-powered device, a full core spinning
nonstop for a feature (websocket push) nothing here actually uses is a real
runtime cost, not just a benchmark curiosity.

### RTC fix (firmware update required)

The onboard RTC was stuck at a 2020 default date. Two dead ends before the real
fix:
- PiSugar's documented kernel-overlay approach (`dtoverlay=i2c-rtc,ds3231` in
  `/boot/firmware/config.txt`) bound a driver but then spammed continuous I2C
  write errors in `dmesg` without ever producing a readable time — reverted.
- `pisugar-server`'s own `rtc_pi2rtc` sync command reported `done` but silently
  did nothing; `rtc_addr` read back as `0x00` (unset) despite the RTC actually
  living at `0x68` on the I2C bus.

**Actual fix: update the PiSugar3 MCU firmware.** It shipped on v1.0.5; updated
via the official installer:
```
curl -fsSL https://cdn.pisugar.com/release/PiSugarUpdate.sh | sudo bash
```
Landed on v1.3.8 (not quite the v1.4.0 PiSugar's docs mention — likely just a
stale doc page, not a problem). After the update, `rtc_addr` correctly
self-reports `0x68` and `rtc_pi2rtc` actually works — RTC time now tracks
system time within a second. This is a real MCU flash (the script pauses for a
"battery must be charged / stop pisugar-server first" confirmation, then
reflashes over I2C) — worth knowing before re-running it, not a config change.

## Display daemon (replaces the one-off button scripts)

The original single/double/long-press scripts each re-init'd the e-Paper panel
from scratch on every call (slow, full-flash every time) and had no way to
receive a pushed status update. Replaced with a persistent systemd service,
`axon-display` (`software/display-service/daemon.py`), installed at
`/home/pi/axon-display/` on display-device:

- Owns the panel for the life of the service — uses `displayPartial` /
  `displayPartBaseImage` for fast, low-flicker updates instead of a full
  refresh every time, with a full refresh every 12 partial updates to bound
  e-ink ghosting.
- Screen registry in `screens.py`: `idle` (home screen, single-tap always
  returns here), plus a double-tap cycle through `diagnostics` → `battery` →
  `network`, each showing page-dot position indicators. `boot` and `shutdown`
  are one-off screens outside the cycle.
- A tiny local HTTP API on `127.0.0.1:8500`:
  - `GET /event/<single|double|long>` — what PiSugar's `button_shell` actually
    calls now (a cheap `curl`, not a whole new Python+PIL+driver process per
    press). Note: this had to be GET+path-segment, not a POST with a JSON
    body — `pisugar-server`'s `button_shell` strips quote characters from the
    command string it stores, so a JSON body doesn't survive round-tripping
    through it.
  - `POST /status` `{"state": "idle"|"running"|"error", "task": "..."}` — the
    actual hook for pushing live agent status, from wherever Claude Code ends
    up running (this box itself, or the Pi4 puck over remote networking). Re-renders
    the idle screen immediately if it's the one currently shown.
  - `GET /health`
- Art hook: `hud.load_art(name, max_size=...)` looks in
  `/home/pi/axon-assets/<name>.png` (or `.bmp`) and dithers it to 1-bit;
  currently wired up for `idle.png` in the status box. Falls back to the
  plain vector text/signal-bars treatment when no art file exists yet - see
  `software/display-service/assets/README.md`.

Old scripts (`prototype_ui.py`, `button_diagnostics.py`, `button_shutdown.py`)
are gone, superseded by the daemon.

## Multi-machine agent status via MQTT (broker-host is the brain)

The user has another always-on box on the network: **broker-host** (`localhost`,
`broker-host@localhost` over SSH) — a genuine Raspberry Pi 5, 8GB RAM, booting off
a real SSD (452GB, 232GB free), already running a substantial stack (Docker,
a Dolt SQL server, remote networking, and notably **Mosquitto**, an MQTT broker). Also
running there: **`built`**, described as an "agentic software factory" — this
*is* what the user calls "Hermes" (Hermes is the agent that codes on broker-host via
`built`). A `built` plugin to publish its own task events is a future item,
not done yet.

Decided **broker-host, not display-device, should be the always-on coordination point**:
display-device is meant to be picked up and carried (that's the point of the puck form
factor + battery), so it's a bad place to host anything other machines depend
on being reachable. broker-host is stationary, wall-powered, and already the more
capable box. Mosquitto was confirmed idle (not used for anything else yet) and
already listening on `0.0.0.0:1883` with `allow_anonymous true` — reachable
from any network machine with zero additional setup.

Considered adding an MCP server on broker-host for this too (agents could then
*query* status, not just push it) — deliberately deferred for now in favor of
plain MQTT pub/sub; simpler, and Hermes/`built` integration isn't happening
yet anyway. Worth revisiting once there's an actual agent-initiated use case
(e.g. "what else is already working on this").

**Contract**: any source publishes to `axon/agents/<source>/status` with a
JSON payload `{"state": "idle"|"running"|"error", "task": "..."}`. display-device's
`axon-display` daemon subscribes to the wildcard `axon/agents/+/status` on
startup (`paho-mqtt`, installed via `apt install python3-paho-mqtt`; note
it's v2.x, which needs `mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)` -
the v1 constructor signature is different) and keeps an in-memory registry
(`state["agents"][source] = {state, task, ts}`) shown on the **AGENTS**
screen (5th in the double-tap cycle: diagnostics → battery → network →
agents), sorted most-recently-updated first, capped to 5 entries. Each row
shows a relative age ("now"/"3m"/"1h"/"2d", computed fresh on every render
from `ts`) right-aligned next to the source name - without this, a report
from an hour ago looked identical to one from ten seconds ago, which is
actively misleading for a "what's happening right now" screen.

**Reconnect resilience verified**: restarted Mosquitto on broker-host directly
and confirmed via `journalctl` that `paho-mqtt`'s background loop
(`loop_start()`) reconnected on its own within ~10s, no restart of
`axon-display` needed - then confirmed a fresh `mosquitto_pub` actually
landed post-reconnect. Not just assumed from the library's docs - tested
for real.

**Real publisher live**: Claude Code hooks are wired up on **workstation**
(see `software/claude-hooks/`) across `SessionStart`, `UserPromptSubmit`,
`PostToolUse`, `Stop`, `SessionEnd` - `PostToolUse` is what makes the task
text representative of actual work ("editing screens.py", "running: git
commit...") rather than a static string or raw prompt text, and lifecycle
events get randomized on-brand phrasing instead of flat strings. Still
needed: the same hooks on the rest of the network machines, and eventually
a `built` plugin for Hermes specifically.

## Open items

1. Install Node/Claude Code and watch real memory usage under load — that result
   decides whether this is a self-contained mini agent box or a status-display
   satellite for the Pi4 puck.
2. Source art for `/home/pi/axon-assets/idle.png` (or wire up more named art
   hooks in `screens.py` as it shows up).
3. Wire up Claude Code hooks on the *other* network machines (only workstation
   is done - see `software/claude-hooks/README.md`), and eventually a `built`
   plugin for Hermes.

## Status

- [x] Fresh OS flashed, SSH key access working (`ssh pi@device.example`)
- [x] remote networking installed and connected (device.example, hostname `display-device`)
- [x] Header soldered, e-Paper HAT + PiSugar moved over and confirmed working
      (SPI + I2C enabled, `epd2in13_V3` demo verified visually on the panel)
- [x] PiSugar3 power manager installed and confirmed reading real battery data
- [x] PiSugar3 RTC fixed via firmware update (v1.0.5 → v1.3.8)
- [x] Real `axon-display` daemon running as a systemd service, partial-refresh
      screens, button-triggered via a local HTTP API, `/status` push endpoint
      ready for an agent to use
- [x] MQTT wired to broker-host (Mosquitto), AGENTS screen added, verified end to
      end with a real `mosquitto_pub` from broker-host
- [ ] Node/Claude Code installed and memory-tested under real usage
- [ ] Art sourced for the idle screen
- [x] Claude Code hooks live on workstation (`SessionStart`/`UserPromptSubmit`/
      `Stop`/`SessionEnd` → `software/claude-hooks/report_status.py`),
      verified end to end including a live panel update
- [ ] Same hooks on the rest of the network machines, and a `built` plugin for Hermes
- [x] NOPASSWD sudo enabled for `pi` on this box (flagged here since it's a
      real security tradeoff: anyone with SSH key access gets root with no friction.
      Reasonable given remote networking-only reachability and single-user, but worth
      remembering if this box's threat model ever changes.)
