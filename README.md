# Axon

A dedicated computer for running agents, in a puck-shaped enclosure with a tilted
monochrome status display — inspired by [Pamir AI's Lapis One](https://www.pamir.ai/),
built from parts on hand: a Raspberry Pi 4 as the brain, an Adafruit Feather RP2040
as a peripheral controller.

See [docs/SPEC.md](docs/SPEC.md) for the full hardware/software spec, and
[docs/PROTOTYPE-PWNAGOTCHI.md](docs/PROTOTYPE-PWNAGOTCHI.md) for a faster-to-bring-up
parallel track that repurposes a spare pwnagotchi (Pi Zero W + e-Paper + PiSugar) as
a remote networking-connected status display while the Pi4 build comes together.

## Layout

- `docs/` — spec and design notes
- `hardware/bom/` — bill of materials
- `hardware/enclosure/` — 3D-printable enclosure design files
- `software/display-service/` — the e-Paper display daemon. Currently holds the
  working version built for the display-device (Zero 2 W) prototype; the Pi4 build will
  need a variant that talks to the Feather over USB-serial instead of driving
  the panel directly.
- `software/feather-firmware/` — CircuitPython firmware for the Feather RP2040 (display, LED bar, IMU, button)
- `software/provisioning/` — first-boot setup scripts (remote networking, agent runtime, display service)
