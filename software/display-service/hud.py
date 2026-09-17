"""Shared drawing primitives for the Axon HUD screens (dark field, chamfered
frame, corner brackets, tick-mark ruler). Pulled out of the original
prototype_ui.py so the PiSugar button-action screens don't each reimplement
the same layout code.

Deliberately does NOT import the Waveshare hardware driver at module level -
only show() needs it, and it's only importable on the Pi itself (the driver
lib lives under ~/e-Paper there). Everything else - canvas setup, all the
drawing primitives, load_art()'s dithering - is plain PIL and runs anywhere,
which is what preview.py relies on to let you iterate on art on a regular
dev machine, no eink hardware or even an SSH connection to the Pi required.

On the Pi, button_shell-invoked scripts run as root, so paths below are
absolute rather than ~-relative (root's HOME isn't /home/pi).
"""
import os
import sys
import time

E_PAPER_LIB = "/home/pi/e-Paper/RaspberryPi_JetsonNano/python/lib"
# Bundled in the repo (fonts/Font.ttc) so this works off the Pi too, not just
# the copy that happens to live inside the cloned e-Paper repo on-device.
FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "Font.ttc")
sys.path.append(E_PAPER_LIB)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image, ImageDraw, ImageFont

font_title = ImageFont.truetype(FONT_PATH, 20)
font_tiny = ImageFont.truetype(FONT_PATH, 9)
font_body = ImageFont.truetype(FONT_PATH, 13)
font_small = ImageFont.truetype(FONT_PATH, 10)

# Native panel size, portrait - no rotation needed by the driver this way.
W, H = 122, 250
BLACK = 0
WHITE = 255
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
    image = Image.new("1", (W, H), BG)
    return image, ImageDraw.Draw(image)


def load_art(name, max_size=None):
    """Load assets/<name>.(png|bmp), dithered to 1-bit. None if not found."""
    for ext in (".png", ".bmp"):
        path = os.path.join(ASSETS_DIR, name + ext)
        if os.path.exists(path):
            img = Image.open(path).convert("L")
            if max_size:
                img.thumbnail(max_size)
            return img.convert("1")
    return None


def page_dots(draw, x, y, count, current):
    for i in range(count):
        cx = x + i * 10
        if i == current:
            draw.ellipse((cx, y, cx + 5, y + 5), fill=FG)
        else:
            draw.ellipse((cx, y, cx + 5, y + 5), outline=FG)


def outer_frame(draw):
    pts = [
        (CHAMFER, 0), (W - 1, 0), (W - 1, H - 1 - CHAMFER),
        (W - 1 - CHAMFER, H - 1), (0, H - 1), (0, CHAMFER),
    ]
    draw.polygon(pts, outline=FG)
    draw.line((CHAMFER, 3, W - 1 - 3, 3), fill=FG)


def header_block(draw, title, subtitle):
    top, bottom = 4, 62
    pts = [
        (CHAMFER, top), (W - 5, top), (W - 5, bottom),
        (5, bottom), (5, top + CHAMFER),
    ]
    draw.polygon(pts, outline=FG)
    draw.text((13, top + 8), title, font=font_title, fill=FG)
    draw.text((13, top + 32), subtitle, font=font_tiny, fill=FG)
    return bottom


def double_rule(draw, y):
    draw.line((0, y, W, y), fill=FG)
    draw.line((0, y + 3, W, y + 3), fill=FG)


def bracket_box(draw, x0, y0, x1, y1, arm=8):
    for (cx, cy, dx, dy) in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
        draw.line((cx, cy, cx + arm * dx, cy), fill=FG)
        draw.line((cx, cy, cx, cy + arm * dy), fill=FG)


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


def show(image, hold=2):
    from waveshare_epd import epd2in13_V3  # only importable/needed on the Pi
    epd = epd2in13_V3.EPD()
    epd.init()
    epd.Clear(0xFF)
    epd.display(epd.getbuffer(image))
    time.sleep(hold)
    epd.sleep()
