# boxen

A dedicated computer for running agents, in a puck-shaped enclosure with a tilted
monochrome status display — inspired by [Pamir AI's Lapis One](https://www.pamir.ai/),
built from parts on hand: a Raspberry Pi 4 as the brain, an Adafruit Feather RP2040
as a peripheral controller.

See [docs/SPEC.md](docs/SPEC.md) for the full hardware/software spec.

## Layout

- `docs/` — spec and design notes
- `hardware/bom/` — bill of materials
- `hardware/enclosure/` — 3D-printable enclosure design files
- `software/display-service/` — Pi4-side daemon that talks to the Feather over USB-serial
- `software/feather-firmware/` — CircuitPython firmware for the Feather RP2040 (display, LED bar, IMU, button)
- `software/provisioning/` — first-boot setup scripts (remote networking, agent runtime, display service)
