# Fast-track prototype: repurposed pwnagotchi

A dormant pwnagotchi on the desk already has a battery (PiSugar) and a display
(Waveshare 2.13" e-Paper) wired up — reusing it gets a physical status device
running this week instead of waiting on the Pi4+Feather build (see
[SPEC.md](SPEC.md)). No SD card in it currently — needs a fresh image.

## What's actually in it

| Part | Confirmed | Notes |
|---|---|---|
| SBC | Raspberry Pi Zero W (original, ARMv6, single-core @1GHz, 512MB RAM) | Not the Zero 2 W — this is the weaker/older chip |
| Display | Waveshare 2.13" e-Paper HAT (~250×122 monochrome, SPI) | Standard pwnagotchi screen |
| Battery | PiSugar (model TBD) | Check silkscreen or I2C address (PiSugar2 ≈ 0x57, PiSugar3 ≈ 0x2d) when you get a chance — not blocking |

## The load-bearing constraint

Node.js officially dropped ARMv6 support after v11 (2018) — the Zero W's chip is
ARMv6. Unofficial community Node builds exist but are unsupported and break across
releases, and even a working Node wouldn't make Claude Code's CLI pleasant on a
single-core 1GHz ARM11 with 512MB RAM.

**Conclusion: this board does not run Claude Code locally.** Re-scoped role: a
remote networking-connected status display and SSH jump point, not the agent's brain. The
actual agent runs elsewhere on the network (the Pi4 puck once it exists, or any other
machine) and pushes status to this box; this box shows it on the e-Paper screen.
That's arguably closer to what Pamir's own screen does anyway — a status indicator,
not a full terminal.

remote networking itself does run here: no official ARMv6 build, but the static binaries
work with a documented workaround (see references below) — may need to pin an
older release (e.g. v1.62.0) if the latest breaks ARMv6 compatibility.

## Software plan

- **OS**: Raspberry Pi OS Lite, 32-bit (still supports ARMv6/Zero W). Flash fresh —
  headless setup via Raspberry Pi Imager's advanced options (SSH + Wi-Fi creds
  preconfigured) since there's no display/keyboard workflow wanted here.
- **Remote access**: remote networking via static binary + systemd service (manual setup,
  not the usual package-manager install — see workaround link below).
- **Display service**: Python (not Node) using Waveshare's official e-Paper Python
  library. Listens on the network for status pushes (`idle`, `running:<task>`,
  `error`) from wherever Claude Code is actually running, renders them as a simple
  status face. Coarse-grained state only — e-ink's refresh speed doesn't suit
  scrolling text or fast animation.
- **PiSugar**: once the model's confirmed, its software (PiSugar-power-manager, a
  small Python/Node-free HTTP service PiSugar themselves provide) reports battery
  level — worth wiring into the status display.

## Open items

1. Spare microSD card (doesn't need to be high-endurance — this box mostly displays
   status, not running write-heavy agent workloads).
2. Confirm PiSugar model when convenient.
3. Once the Pi4 puck exists, define the status-push protocol between them (simple
   HTTP POST over remote networking is probably enough).

## References

- [remote networking on Pi Zero W install workaround](https://gist.github.com/StrixROX/5b1b035351a00e5498ad6099e961c64d)
- [Node.js on ARMv6 — unofficial builds](https://gist.github.com/mandrean/71f2cbf707025a5983c0fc04d78f3e9a) (context only — not using Node here)
