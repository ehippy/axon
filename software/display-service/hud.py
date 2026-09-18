"""Shared drawing primitives for the Axon HUD (dark field, vertical title,
tick-mark ruler). Pulled out of the original prototype_ui.py so screens
don't reimplement the same layout code.

Targets the PiSugar Whisplay HAT (240x280 color LCD, ST7789P3, driven by
PiSugar's own runtime/whisplay.py - see software/display-service/whisplay/)
- replaced the Waveshare e-Paper HAT this was originally built for. Canvas
is full RGB now, not 1-bit, but the drawing code barely changed: FG/BG are
just RGB tuples instead of 0/255, and load_art() no longer needs to dither
to black/white.

Deliberately does NOT import the Whisplay hardware driver at module level -
only show() needs it, and it's only importable on the Pi itself. Everything
else - canvas setup, all the drawing primitives, load_art() - is plain PIL
and runs anywhere, which is what preview.py relies on to let you iterate on
art on a regular dev machine, no hardware or even an SSH connection needed.

On the Pi, button_shell-invoked scripts run as root, so paths below are
absolute rather than ~-relative (root's HOME isn't /home/pi).
"""
import os
import sys
import time

WHISPLAY_RUNTIME = "/home/pi/Whisplay/runtime"
# Bundled in the repo (fonts/Font.ttc) so this works off the Pi too.
FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "Font.ttc")
sys.path.append(WHISPLAY_RUNTIME)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image, ImageDraw, ImageFont

font_title = ImageFont.truetype(FONT_PATH, 20)
font_wordmark = ImageFont.truetype(FONT_PATH, 30)
font_watermark = ImageFont.truetype(FONT_PATH, 64)
font_tiny = ImageFont.truetype(FONT_PATH, 11)
font_body = ImageFont.truetype(FONT_PATH, 15)
font_small = ImageFont.truetype(FONT_PATH, 12)

# The panel's fixed physical size (matches WhisplayBoard.LCD_WIDTH/HEIGHT) -
# do not confuse with W, H below. Unlike the old e-ink driver, Whisplay's
# draw_image() has no rotation smarts of its own (its MADCTL orientation is
# set once at init and never revisited) - to show content rotated, we have
# to compose in a logical canvas of the *other* orientation and rotate the
# whole finished frame ourselves before it goes to hardware. See
# rotate_for_display()/to_hardware_frame() below.
PANEL_W, PANEL_H = 240, 280

# Logical composition canvas - landscape, since content is rotated 90deg
# clockwise onto the physical (portrait) panel before display.
W, H = 280, 240
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
BG = BLACK
FG = WHITE
CHAMFER = 10

# Status colors - the single source of truth for both the on-screen status
# dot/row coloring and the physical RGB LED (see daemon.py's _update_led),
# so the two channels can't drift into disagreeing with each other. Full
# brightness, meant for on-screen use directly; dim() below scales these
# down for the LED, which is much more glare-y at full saturation than a
# same-color pixel on the panel.
STATUS_COLORS = {
    "error": (255, 60, 60),
    "running": (70, 150, 255),
    "idle": (90, 200, 120),
}
STATUS_EMPTY = (90, 90, 90)  # nothing reporting


def status_color(agent_state):
    return STATUS_COLORS.get(agent_state, STATUS_COLORS["idle"])


def aggregate_status(agents):
    """One status bucket for the whole agents dict - same priority order
    the LED uses: error beats running beats idle. None if agents is empty."""
    if not agents:
        return None
    states = [a.get("state", "idle") for a in agents.values()]
    if "error" in states:
        return "error"
    if "running" in states:
        return "running"
    return "idle"


def dim(color, factor=0.35):
    return tuple(int(c * factor) for c in color)


def status_dot(draw, x, y, color, r=5):
    draw.ellipse((x - r, y - r, x + r, y + r), fill=color)

# Drop custom art here - see assets/README.md. Looked up by name with no
# extension; .png tried first, then .bmp. Missing files are just skipped by
# callers (screens fall back to vector-drawn primitives), so this directory
# can stay empty/partially filled while art gets sourced.
# Overridable via AXON_ASSETS_DIR - preview.py points this at a local staging
# folder so draft art can be iterated on without touching the Pi at all.
ASSETS_DIR = os.environ.get("AXON_ASSETS_DIR", "/home/pi/axon-assets")


def new_canvas():
    image = Image.new("RGB", (W, H), BG)
    return image, ImageDraw.Draw(image)


def load_art(name, max_size=None):
    """Load assets/<name>.(png|bmp), real color now - no more 1-bit
    dithering, the LCD does actual color. None if not found."""
    for ext in (".png", ".bmp"):
        path = os.path.join(ASSETS_DIR, name + ext)
        if os.path.exists(path):
            img = Image.open(path).convert("RGB")
            if max_size:
                img.thumbnail(max_size)
            return img
    return None


def page_dots(draw, x, y, count, current):
    for i in range(count):
        cx = x + i * 10
        if i == current:
            draw.ellipse((cx, y, cx + 5, y + 5), fill=FG)
        else:
            draw.ellipse((cx, y, cx + 5, y + 5), outline=FG)


def vertical_text(image, text, font, x=0, y=0):
    """Paste `text` rotated 90deg clockwise, top-left corner of the rotated
    block at (x, y). Returns the (width, height) of the placed block so
    callers can lay out whatever comes next around it.
    """
    bbox = font.getbbox(text)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tmp = Image.new("RGB", (tw, th), BG)
    tmp_draw = ImageDraw.Draw(tmp)
    tmp_draw.text((-bbox[0], -bbox[1]), text, font=font, fill=FG)
    rotated = tmp.rotate(-90, expand=True)  # negative = clockwise
    image.paste(rotated, (x, y))
    return rotated.size


def watermark(draw, text, opacity=0.165, margin_x=20, margin_y=0):
    """Big, dim logo mark snapped to the bottom-right corner, meant to be
    drawn first so real content sits on top of it. "Opacity" here is just
    alpha-over-black math (result = fg * opacity), not real alpha
    compositing - correct as long as BG stays pure black, much simpler
    than layering an RGBA overlay for a background that never changes.

    margin_x clears the panel's rounded-corner radius on the right (the
    driver's CornerHeight = 20) the same way the battery icon needed;
    margin_y is 0 by design - flush against the bottom edge, which isn't
    part of the curved corner region the way the right edge near the
    bottom-right corner is.
    """
    bbox = font_watermark.getbbox(text)
    x = W - margin_x - bbox[2]
    y = H - margin_y - bbox[3]
    draw.text((x, y), text, font=font_watermark, fill=dim(FG, opacity))


def battery_icon(draw, x, y, pct, charging=False, w=18, h=9):
    """Tight little battery glyph: outline + nub + proportional fill, plus a
    small cutout "+" mark over the fill when charging. No text - the icon
    alone is meant to carry level and charging state.
    """
    draw.rectangle((x, y, x + w, y + h), outline=FG)
    nub_h = h // 2
    draw.rectangle((x + w + 1, y + (h - nub_h) // 2, x + w + 2, y + (h - nub_h) // 2 + nub_h), fill=FG)
    fill_w = max(0, int((w - 2) * max(0, min(100, pct)) / 100))
    if fill_w:
        draw.rectangle((x + 1, y + 1, x + 1 + fill_w, y + h - 1), fill=FG)
    if charging:
        cx, cy = x + w // 2, y + h // 2
        draw.line((cx - 2, cy, cx + 2, cy), fill=BG)
        draw.line((cx, cy - 2, cx, cy + 2), fill=BG)


def double_rule(draw, y, x0=0, x1=None):
    x1 = W if x1 is None else x1
    draw.line((x0, y, x1, y), fill=FG)
    draw.line((x0, y + 3, x1, y + 3), fill=FG)


def tick_ruler(draw, x, y0, y1):
    y = y0
    i = 0
    while y <= y1:
        length = 6 if i % 4 == 0 else 3
        draw.line((x, y, x + length, y), fill=FG)
        y += 7
        i += 1


def signal_bars(draw, x, y):
    heights = (4, 7, 10, 13, 16)
    for i, h in enumerate(heights):
        bx = x + i * 6
        draw.rectangle((bx, y + 16 - h, bx + 3, y + 16), fill=FG)


def to_rgb565(image):
    """Pack a PIL RGB image into the raw big-endian RGB565 byte buffer
    WhisplayBoard.draw_image() expects. Pure Python - no numpy needed, and
    numpy would be one more thing competing for display-device's tight 512MB.
    """
    data = image.convert("RGB").tobytes()
    out = bytearray(len(data) // 3 * 2)
    for i in range(0, len(data), 3):
        r, g, b = data[i], data[i + 1], data[i + 2]
        val = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        j = i // 3 * 2
        out[j] = (val >> 8) & 0xFF
        out[j + 1] = val & 0xFF
    return bytes(out)


def rotate_for_display(image):
    """Rotate a logical (W x H) composed frame 90deg clockwise into the
    panel's fixed physical (PANEL_W x PANEL_H) orientation. Negative angle
    = clockwise in PIL's convention. Callers that need to reason about
    on-screen corners should do it in physical terms, not logical ones -
    a corner that's safe pre-rotation may not be post-rotation.
    """
    rotated = image.rotate(-90, expand=True)
    assert rotated.size == (PANEL_W, PANEL_H), f"rotated size {rotated.size} != panel ({PANEL_W}, {PANEL_H})"
    return rotated


def to_hardware_frame(image):
    """Logical composed frame -> ready-to-send RGB565 bytes, rotated to
    match the panel's fixed physical orientation."""
    return to_rgb565(rotate_for_display(image))


def show(image, hold=2):
    from whisplay import WhisplayBoard  # only importable/needed on the Pi
    board = WhisplayBoard()
    board.set_backlight(70)
    board.draw_image(0, 0, board.LCD_WIDTH, board.LCD_HEIGHT, to_hardware_frame(image))
    time.sleep(hold)
    board.cleanup()
