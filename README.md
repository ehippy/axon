# boxen

A dedicated Pi5-based computer for running agents, in a puck-shaped enclosure with
a tilted monochrome status display — inspired by [Pamir AI's Lapis One](https://www.pamir.ai/),
built around commodity Raspberry Pi 5 parts instead of custom silicon.

See [docs/SPEC.md](docs/SPEC.md) for the full hardware/software spec.

## Layout

- `docs/` — spec and design notes
- `hardware/bom/` — bill of materials
- `hardware/enclosure/` — 3D-printable enclosure design files
- `software/display-service/` — SPI display + LED status daemon
- `software/provisioning/` — first-boot setup scripts (remote networking, agent runtime, display service)
