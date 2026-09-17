# Display art

Drop custom art here as it gets sourced, deployed to `/home/pi/axon-assets/`
on display-device (see `hud.ASSETS_DIR`). Nothing is required - screens fall back to
plain vector-drawn primitives (text/shapes) for anything missing.

Currently looked up:

- `idle.png` (or `.bmp`) - shown in the status box on the idle screen instead
  of the plain "IDLE" text + signal bars. Loaded via `hud.load_art("idle",
  max_size=(70, 70))` - anything larger gets thumbnailed down, so square
  source art around 70-140px works well. Gets dithered to 1-bit on load, so
  high-contrast line art reads better than soft gradients on this panel.

To wire up a new named piece of art in another screen, call
`hud.load_art("<name>")` from that screen's render function in `screens.py`.
