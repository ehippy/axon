# Bill of materials

Status: partial — filling in as parts get sourced/confirmed. See [../../docs/SPEC.md](../../docs/SPEC.md)
for the reasoning behind each choice.

## Already on hand

| Part | Role | Notes |
|---|---|---|
| Raspberry Pi 4B | Main compute — remote networking, Claude Code, display/voice services | RAM size unknown, need to check |
| Adafruit Feather RP2040 | Peripheral controller — display, LED bar, IMU, button | Chosen over the bare RP2040 and QT Py RP2040 for GPIO count + onboard NeoPixel + STEMMA QT |
| Pi Zero 2 W | Spare | Not in v1 scope |
| Bare RP2040 board, QT Py RP2040 | Spares | Not in v1 scope; QT Py earmarked for a possible future compact sensor node |

## To source

| Part | Choice | Source | Status |
|---|---|---|---|
| Display | Adafruit SHARP Memory Display Breakout, 2.7" 400×240 Monochrome (product 4694) | [DigiKey, part 13157994](https://www.digikey.com/en/products/detail/adafruit-industries-llc/4694/13157994) | Confirmed in stock, not yet ordered |
| Boot storage | USB3 SSD (any reputable small SATA/NVMe-in-enclosure or dedicated USB SSD) | Not yet chosen | Not yet sourced |
| Voice HAT | Seeed ReSpeaker 2-Mics Pi HAT | Seeed / distributors (Mouser, DigiKey, Seeed direct) | Not yet sourced |
| Speaker | Small 3W 8Ω speaker w/ JST-PH 2.0 connector (matches ReSpeaker HAT's speaker port) | Any (Adafruit/Seeed both sell matching ones) | Not yet sourced |
| IMU | STEMMA QT IMU breakout (MPU6050 or BNO055) | Adafruit | Not yet sourced |
| Power button | Plain momentary push button, wired to the Feather | Any | Not yet sourced |
| Hard power cutoff | Inline USB-C power switch/cable | Any | Not yet sourced |
| Cooling | Pi4-compatible heatsink+fan case or fan shim | Not yet chosen | Not yet sourced |
| Stacking header | 40-pin female stacking header (so the voice HAT + external GPIO breakout can coexist) | Any | Not yet sourced |
