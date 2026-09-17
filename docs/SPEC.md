# boxen — a Pi5-based agent puck

Own-build alternative to Pamir AI's Lapis One (https://www.pamir.ai/), wrapped around a
Raspberry Pi 5 instead of a custom Rockchip board. Same "puck with a tilted-up mono
screen" form factor; scoped-down feature set (no Agent KVM, no fingerprint sensor).

Pamir's hardware/OS/SDK is closed — no GitHub, SDK repo, or license published on their
site — so this is a clean-room build off the publicly visible form factor and specs,
not a port of anything of theirs.

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

Going straight for the real thing: **Adafruit SHARP Memory Display Breakout — 2.7"
400×240 Monochrome** (product 4694, ~$45) — the same panel family Pamir uses, already
broken out to a solder-friendly header (`VIN, GND, SCLK, MOSI, CS, EXTMODE, EXTCOMIN,
DISP`) rather than bare FPC. Currently out of stock at Adafruit; check Pimoroni,
Mouser, or DigiKey for the bare LS027B7DH01 panel as a fallback source, or wait for a
restock. Possibly already own one — if so, check whether it's actually the smaller
1.3" 168×144 variant (product 3502); silkscreen on the board will say which.

- SPI interface (SCLK/MOSI/CS) straight to Pi5 SPI0, plus a GPIO toggling EXTCOMIN at
  ~1Hz (software PWM/timer) to keep the panel refreshed — factor this into the
  display-service design.
- Enclosure bezel cutout sized for whichever variant we end up with (400×240 vs
  168×144 changes the puck's screen footprint significantly).

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

- v1: **wall power only** — USB-C PD straight into the Pi5 (official Pi5 PSU, 5V/5A).
  No battery/UPS in v1: it adds charge-management and enclosure complexity for zero
  validation value while we're still proving out the mechanical/software design.
- Later, optional: a Pi5 UPS HAT (PiSugar, Geekworm, etc.) with a modest single-cell
  Li-ion (~2000mAh) — enough for graceful shutdown on unplug and a few minutes of
  "pick it up and walk to another room" portability. (For reference, Pamir's spec
  lists a 3,300mAh battery — it is not a wall-only device like our v1.)

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

3D-printed (own printer, own filament — no print-service constraints), two main parts:
1. **Base shell** — houses Pi5 + HAT stack, front light-bar slot, side/back cutouts
   for USB-C, GPIO breakout, and airflow for the active cooler. Leave room to retrofit
   a UPS HAT later without redesigning the shell.
2. **Screen bezel** — tilted ~30-40°, hinge-free (fixed angle, printed as one piece
   with the base or as a screwed-on wedge), holds the prototype display first, sized
   to also fit the 2.7" Sharp panel's cutout for the later swap.

v1 target: FDM-printable (PETG or ABS) in white + an orange accent bezel to echo
the reference photos, no injection-molding-only geometry.

## Open decisions (need your input before BOM lock)

1. **Budget ballpark** — rough total for parts (Pi5 you may already have; NVMe HAT +
   drive ~$40-70; prototype display + LEDs + filament likely $20-40 more; the real
   Sharp panel adds ~$45 later).
2. **Which Pi5 you're using** — which one of the Pis you have on hand is it (RAM size,
   and whether it's already got an NVMe HAT/case on it), so the enclosure and BOM fit
   the actual board instead of a generic spec.
3. **Prototype display** — what do you actually have sitting around (ST7789? an
   e-paper HAT? something else?) so I can wire the display-service software to the
   real part instead of a generic placeholder.
