# boxen — a Pi5-based agent puck

Own-build alternative to Pamir AI's Lapis One (https://www.pamir.ai/), wrapped around a
Raspberry Pi 5 instead of a custom Rockchip board. Same "puck with a tilted-up mono
screen" form factor; scoped-down feature set (no Agent KVM, no fingerprint sensor).

## Goals (from initial scoping)

- **Form factor**: puck — low flat base, monochrome display tilted back ~100-110°
  in a bezel, sitting on the desk most of the time but light enough to pick up and
  carry (not a battery-all-day portable).
- **Must-have**: GPIO breakout for hardware hacking, remote access baked in
  (remote networking) so the agent is reachable/SSH-able from anywhere.
- **Explicitly out of scope for v1**: Agent KVM (USB capture + HID emulation),
  fingerprint sensor, immutable/A-B signed OS (nice-to-have later, not v1).

## Compute

| Part | Choice | Notes |
|---|---|---|
| SBC | Raspberry Pi 5 (8GB) | 8GB headroom for running local models / agent tooling alongside Claude Code |
| Storage | NVMe SSD via M.2 HAT (PCIe) + microSD for initial boot | NVMe for the root fs — quieter and far more durable than SD under agent workloads (frequent writes, logs, git) |
| Cooling | Official Active Cooler or low-profile equivalent | Puck is short (~30-35mm) — cooler height is the tightest constraint in the enclosure |

## Display

Pamir uses a Sharp memory-in-pixel monochrome LCD (240×400, ultra-low-power,
always-on-capable). Closest easily-sourceable equivalent for a Pi5 build:

- **Sharp LS013B7DH03** (128×128) or **LS027B7DH01** (400×240, matches Pamir's spec)
  memory LCD, SPI interface — near-zero power when static, good sunlight readability,
  matches the "always-visible status face" aesthetic.
- Driven directly off Pi5 SPI0, plus a GPIO for the display EXTCOMIN toggle (can be
  done in software via PWM or a small always-on 555/timer if we don't want to burn a
  CPU thread on it).
- Fallback if sourcing the Sharp panel is a pain: a small round or rectangular
  transflective/e-paper panel — worse animation, but even lower power and cheaper.

Mounted in a 3D-printed bezel tilted ~30-40° off the base's rear edge, matching the
photo reference.

## GPIO

Pi5's 40-pin header is broken out to a small panel on the **back edge of the base**
(not magnetic like Pamir — just a standard 2×20 header or Pi HAT-compatible
mini-breakout, since that's dramatically simpler to build and service). Enclosure
has a cutout so a ribbon cable or HAT can attach without disassembly.

## Status light bar

Front-edge LED light bar (matches the photo) — an addressable strip (WS2812B/SK6812,
a handful of LEDs) diffused behind a frosted acrylic/PETG light pipe, driven off a
Pi5 GPIO pin (PWM or one-wire). Used for at-a-glance agent state (idle/working/error)
independent of what's on the screen.

## Power

- Primary: USB-C PD input to the Pi5 (5V/5A via official Pi5 PSU, or a PD trigger
  board if we want 9V/3A like Pamir for thinner cabling).
- Small UPS: a Pi5 UPS HAT (e.g. PiSugar or Geekworm UPS) with a modest single-cell
  Li-ion (~2000mAh) — enough for graceful shutdown on unplug and a few minutes of
  "pick it up and walk to another room" portability, not all-day battery life. This
  matches "mobile but happily lives on a desk" rather than Pamir's 3300mAh target.

## Software stack

- **OS**: Raspberry Pi OS Lite (Debian 12, arm64) as the base — swap for a hardened
  read-only-root setup later if v1 proves out.
- **Remote access**: remote networking installed and enrolled at first boot; `remote-networking ssh`
  enabled so no separate SSH key management is needed once it's on the network.
- **Agent runtime**: Claude Code (or the Agent SDK) running as a systemd service,
  logs to journald.
- **Display service**: small Python/Rust daemon owning the SPI display + LED bar,
  exposing a local socket/HTTP API the agent runtime posts status updates to
  (`idle`, `running: <task>`, `error`, `sleeping`). Mirrors Pamir's sleeping-robot
  status face.
- **Provisioning**: a single `first-boot.sh` (or Ansible playbook) that installs
  remote networking, the display service, and the agent runtime from a fresh Pi OS Lite
  flash — so the whole software side is reproducible from a blank SD card.

## Enclosure

3D-printed, two main parts:
1. **Base shell** — houses Pi5 + HAT stack + UPS + battery, front light-bar slot,
   side/back cutouts for USB-C, GPIO breakout, and airflow for the active cooler.
2. **Screen bezel** — tilted ~30-40°, hinge-free (fixed angle, printed as one piece
   with the base or as a screwed-on wedge), holds the SPI display + diffuser.

v1 target: FDM-printable (PETG or ABS) in white + an orange accent bezel to echo
the reference photos, no injection-molding-only geometry.

## Open decisions (need your input before BOM lock)

1. **Budget ballpark** — rough total you're comfortable with for parts (Pi5 alone
   is ~$80, NVMe HAT + drive another ~$40-70, display + UPS + LEDs + printed parts
   likely $60-100 more).
2. **Do you already have a Pi5**, or should the BOM start from zero?
3. **Display sourcing** — comfortable hand-soldering/wiring a bare Sharp memory LCD
   breakout, or would you rather use an easier-to-source small SPI LCD/e-paper even
   if it's visually a bit different from Pamir's panel?
4. **3D printing** — do you have access to a printer, or does this need to go out
   to a print service (affects tolerances/material choices)?
