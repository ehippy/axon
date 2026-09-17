# Axon — a Pi4 + RP2040 agent puck

Own-build alternative to Pamir AI's Lapis One (https://www.pamir.ai/), built from parts
already on hand: a Raspberry Pi 4 as the brain, an Adafruit Feather RP2040 as a
peripheral controller. Same "puck with a tilted-up mono screen" form factor;
scoped-down feature set (no Agent KVM, no fingerprint sensor).

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
| SBC | Raspberry Pi 4B (already owned) | RAM size unknown — no silkscreen marking. Check with `cat /proc/cpuinfo \| grep Revision` and look up the revision code, or `vcgencmd get_config total_mem`. Doesn't block starting — the software stack (remote networking + Claude Code + lightweight display/voice services) should fit even on a 2GB unit; revisit only if it turns out to be 1GB. |
| Storage | microSD for v1 prototype | Skipping USB-SSD/NVMe for now to keep the prototype simple and free up a USB port for the Feather; get a high-endurance card (e.g. SanDisk High Endurance / Industrial) given agent workloads write a lot (logs, git). Revisit USB-SSD boot later if card wear or reliability becomes a real problem. |
| Cooling | Pi4-specific heatsink+fan case or fan shim (not Pi5's Active Cooler, which is a different mount) | Puck is short (~30-35mm) — cooler height is the tightest constraint in the enclosure |

The Pi Zero 2 W and the spare bare RP2040 board stay out of the core build for now —
reserved as spares if the Pi4 turns out too weak, or for some future stretch goal
(e.g. a second small sensor node).

## Display

Going straight for the real thing: **Adafruit SHARP Memory Display Breakout — 2.7"
400×240 Monochrome** (product 4694, ~$45) — the same panel family Pamir uses, already
broken out to a solder-friendly header (`VIN, GND, SCLK, MOSI, CS, EXTMODE, EXTCOMIN,
DISP`) rather than bare FPC. Out of stock direct from Adafruit, but in stock via
DigiKey ([DigiKey 4694, part 13157994](https://www.digikey.com/en/products/detail/adafruit-industries-llc/4694/13157994))
— that's the source to order from.

- SPI interface (SCLK/MOSI/CS) wired to the **Feather RP2040** (see below), not
  directly to the Pi4 — Adafruit's own `adafruit_sharpmemorydisplay` CircuitPython
  library runs natively on the Feather and already handles the EXTCOMIN refresh
  toggling internally, so none of that timing-sensitive work needs to happen on Linux.
- Enclosure bezel cutout sized for the 400×240 panel.

Mounted in a 3D-printed bezel tilted ~30-40° off the base's rear edge, matching the
photo reference.

## Peripheral controller

An **Adafruit Feather RP2040** (already owned — chosen over the bare RP2040 board and
the QT Py RP2040 for its GPIO count, onboard NeoPixel, and STEMMA QT port) runs
CircuitPython and owns everything that's fussy/real-time, connected to the Pi4 over a
single USB cable (enumerates as USB-serial):

- Drives the Sharp memory LCD (SPI + EXTCOMIN timing).
- Drives the front-edge WS2812/SK6812 LED light bar.
- Reads the IMU over its onboard STEMMA QT connector (plug-and-go with a STEMMA QT
  IMU breakout, no soldering).
- Debounces the physical power button and detects short-press vs. long-press.

Protocol: simple line-based serial commands from the Pi4 (`status idle`, `status
running:<task>`, `status error`) and events back from the Feather (`button:short`,
`button:long`, `motion`). Keeps all of the twitchy hardware timing off Linux and
keeps the Pi4's 40-pin header almost entirely free (see GPIO, below).

The bare RP2040 and the QT Py RP2040 stay spares — QT Py's small size makes it a
good candidate if we ever want a second, more tucked-away sensor node (e.g. for a
stretch-goal fingerprint or NFC module).

## GPIO

Pi4's 40-pin header is broken out to a small panel on the **back edge of the base**
(not magnetic like Pamir — just a standard 2×20 header or Pi HAT-compatible
mini-breakout, since that's dramatically simpler to build and service). Enclosure
has a cutout so a ribbon cable or HAT can attach without disassembly. Now that the
display, LED bar, and IMU all live on the Feather instead, the header only has the
voice HAT on it — leaving far more of it free for actual hacking than in the
Pi5/HAT-stack version of this plan.

Since the voice HAT below (also a 40-pin board) sits on the same header, it needs to
be one with a pass-through/stacking header so the external breakout still exposes the
pins the HAT doesn't use.

## Add-ons (in scope for v1)

Pamir's spec sheet has a few things worth pulling in beyond the core puck:

- **Voice I/O**: Seeed **ReSpeaker 2-Mics Pi HAT** — dual-mic array + WM8960 codec,
  JST speaker output (pairs with a small 3W 8Ω speaker), and it happens to also
  include 2 onboard push buttons and 3 APA102 RGB LEDs for free. Gives the puck an
  actual voice interface (wake word / talk to the agent) instead of typing only.
- **IMU**: a STEMMA QT IMU breakout (MPU6050 or BNO055) plugged straight into the
  Feather's STEMMA QT port — cheap "picked up / moved" motion sensing, could drive a
  display-state change (e.g. wake from the sleeping-robot idle screen).
- **Power button**: a physical momentary button wired to the Feather, which debounces
  it and tells the Pi4 (over the same USB-serial link) to run a clean `shutdown -h`
  on a short press. For a true hard power cutoff, the simplest v1 answer is a plain
  inline switch on the USB-C power cable itself — no MOSFET power-switch circuit to
  design. Auto hard-cutoff-after-shutdown (PiSugar/On-Off-SHIM style) is a fine
  later upgrade, not needed for v1.

Explicitly still out of scope: fingerprint sensor, NFC, dedicated NPU accelerator,
Agent KVM — all deferred as later stretch goals, not v1.

## Status light bar

Front-edge LED light bar (matches the photo) — an addressable strip (WS2812B/SK6812,
a handful of LEDs) diffused behind a frosted acrylic/PETG light pipe, driven by the
Feather RP2040 (see Peripheral controller, above). Used for at-a-glance agent state
(idle/working/error) independent of what's on the screen. Separate from — not
replaced by — the voice HAT's 3 onboard APA102 LEDs and the Feather's own onboard
NeoPixel, which are small/on-board and not positioned for a diffused front-edge bar;
those are free to repurpose for something else (mic level/mute indicator, Feather
heartbeat, say).

## Power

- v1: **wall power only** — official Pi4 USB-C PSU (5V/3A). The Feather RP2040 is
  bus-powered off its USB connection to the Pi4, no separate supply needed.
  No battery/UPS in v1: it adds charge-management and enclosure complexity for zero
  validation value while we're still proving out the mechanical/software design.
- Later, optional: a Pi4 UPS HAT (PiSugar, Geekworm, etc.) with a modest single-cell
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
- **Display service**: small daemon on the Pi4 that owns the USB-serial link to the
  Feather RP2040 and exposes a local socket/HTTP API the agent runtime posts status
  updates to (`idle`, `running: <task>`, `error`, `sleeping`) — translates those into
  the serial commands the Feather's CircuitPython firmware understands. Mirrors
  Pamir's sleeping-robot status face.
- **Feather firmware**: CircuitPython script owning the actual display/LED/IMU/button
  hardware, taking commands over USB-serial and emitting button/motion events back.
- **Provisioning**: a single `first-boot.sh` (or Ansible playbook) that installs
  remote networking, the display service, and the agent runtime from a fresh Pi OS Lite
  flash — so the whole software side is reproducible from a blank SD card.

## Enclosure

3D-printed (own printer, own filament — no print-service constraints), two main parts:
1. **Base shell** — houses the Pi4 + voice HAT stack, the Feather RP2040 (tucked near
   the display/LED-bar wiring since it drives both), front light-bar slot, side/back
   cutouts for USB-C, GPIO breakout, and airflow for the cooling fan. Leave room to
   retrofit a UPS HAT later without redesigning the shell.
2. **Screen bezel** — tilted ~30-40°, hinge-free (fixed angle, printed as one piece
   with the base or as a screwed-on wedge), sized for the 2.7" Sharp panel.

v1 target: FDM-printable (PETG or ABS) in white + an orange accent bezel to echo
the reference photos, no injection-molding-only geometry.

## Open decisions

1. **Pi4 RAM size** — unknown for now, doesn't block starting (see Compute above);
   check `cat /proc/cpuinfo | grep Revision` when convenient.
2. **Budget ballpark** — rough total for the parts not already on hand: USB3 SSD
   ~$25-40, ReSpeaker HAT + speaker ~$35-50, STEMMA QT IMU ~$10-15, Sharp display
   ~$45, cooling case + misc wiring/connectors ~$15-25.
