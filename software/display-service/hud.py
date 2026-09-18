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
font_tiny = ImageFont.truetype(FONT_PATH, 11)
font_body = ImageFont.truetype(FONT_PATH, 15)
font_small = ImageFont.truetype(FONT_PATH, 12)

# Native portrait, matches WhisplayBoard.LCD_WIDTH/LCD_HEIGHT. 240px is
# already roomy (vs. the old e-ink's 122px), so no need to rotate sideways
# the way the e-ink screen needed to.
W, H = 240, 280
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
BG = BLACK
FG = WHITE
CHAMFER = 10

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


def header_block(image, draw, title):
    """Draws the vertical title flush left, centered in the panel's height.
    Returns next_x: where content can start, to the title's right.
    """
    bbox = font_wordmark.getbbox(title)
    title_extent = bbox[2] - bbox[0]  # becomes the vertical extent post-rotation
    y = max(4, (H - title_extent) // 2)
    tw, _ = vertical_text(image, title, font_wordmark, x=4, y=y)
    return 4 + tw + 12


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


def show(image, hold=2):
    from whisplay import WhisplayBoard  # only importable/needed on the Pi
    board = WhisplayBoard()
    board.set_backlight(70)
    board.draw_image(0, 0, board.LCD_WIDTH, board.LCD_HEIGHT, to_rgb565(image))
    time.sleep(hold)
    board.cleanup()
