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
| SBC | Raspberry Pi Zero 2 W (quad-core Cortex-A53, 512MB RAM) | Swapped in place of the pwnagotchi's original Pi Zero W |
| Display | Waveshare 2.13" e-Paper HAT (~250×122 monochrome, SPI) | Moved over from the pwnagotchi |
| Battery | PiSugar (model TBD) | Check silkscreen or I2C address (PiSugar2 ≈ 0x57, PiSugar3 ≈ 0x2d) when convenient — not blocking. Confirm it's electrically fine on Zero 2 W, not just mechanically — most Zero-form PiSugars are, but worth a quick check against PiSugar's compatibility list. |
| GPIO header | Needs soldering | Zero 2 W ships without a populated header |

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

- **OS**: Raspberry Pi OS Lite, 32-bit or 64-bit (Zero 2 W supports both — 32-bit
  has a smaller memory footprint, worth preferring given the 512MB ceiling). Flash
  fresh — headless setup via Raspberry Pi Imager's advanced options (SSH + Wi-Fi
  creds preconfigured).
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

## Open items

1. Solder a 40-pin header onto the Zero 2 W.
2. Spare microSD card (doesn't need to be high-endurance if this stays a
   status-display role; get a decent one if it ends up running Claude Code locally).
3. Confirm PiSugar model and its Zero 2 W compatibility.
4. Once it's booted: actually test Claude Code under real usage and watch memory —
   that result decides whether this is a self-contained mini agent box or a
   status-display satellite for the Pi4 puck.
