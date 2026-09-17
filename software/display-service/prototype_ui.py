#!/usr/bin/env python3
"""One-off prototype UI for the Waveshare 2.13" e-Paper HAT (V3) on display-device.
Not the real display service yet - a vertical, dark HUD-styled status mockup
(black field, chamfered frame, inverted header slab, corner-bracket readout,
tick-mark ruler) to prove out the look before wiring it to live agent/battery
state.
"""
import os
import sys
import time
from datetime import datetime

sys.path.append(os.path.expanduser("~/e-Paper/RaspberryPi_JetsonNano/python/lib"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from waveshare_epd import epd2in13_V3
from PIL import Image, ImageDraw, ImageFont
from pisugar_client import get_status as get_battery_status

FONT_PATH = os.path.expanduser("~/e-Paper/RaspberryPi_JetsonNano/python/pic/Font.ttc")
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


def outer_frame(draw):
    # chamfered (corner-cut) outer border - top-left and bottom-right corners cut
    pts = [
        (CHAMFER, 0), (W - 1, 0), (W - 1, H - 1 - CHAMFER),
        (W - 1 - CHAMFER, H - 1), (0, H - 1), (0, CHAMFER),
    ]
    draw.polygon(pts, outline=FG)
    # echo the chamfer with a short second line just inside it
    draw.line((CHAMFER, 3, W - 1 - 3, 3), fill=FG)


def header_block(draw):
    # outlined slab across the top - no fill, stays part of the black field
    top, bottom = 4, 62
    pts = [
        (CHAMFER, top), (W - 5, top), (W - 5, bottom),
        (5, bottom), (5, top + CHAMFER),
    ]
    draw.polygon(pts, outline=FG)
    draw.text((13, top + 8), "BOXEN", font=font_title, fill=FG)
    draw.text((13, top + 32), "AGENT UNIT // 01", font=font_tiny, fill=FG)
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


def main():
    epd = epd2in13_V3.EPD()
    epd.init()
    epd.Clear(0xFF)

    image = Image.new("1", (W, H), BG)
    draw = ImageDraw.Draw(image)

    outer_frame(draw)
    y = header_block(draw)
    double_rule(draw, y + 6)

    box_top, box_bottom = y + 16, 190
    bracket_box(draw, 10, box_top, W - 10, box_bottom)
    tick_ruler(draw, W - 14, box_top + 4, box_bottom - 4)

    draw.text((18, box_top + 10), "STATUS", font=font_small, fill=FG)
    draw.text((18, box_top + 24), "IDLE", font=font_title, fill=FG)
    signal_bars(draw, 18, box_top + 58)

    double_rule(draw, box_bottom + 12)

    hostname = os.uname()[1]
    now = datetime.now().strftime("%H:%M:%S")
    try:
        batt = get_battery_status()
        batt_str = f"BATT {batt['percent']}%{' CHG' if batt['charging'] else ''}"
    except OSError:
        batt_str = "BATT --%  (pisugar-server unreachable)"
    draw.text((8, box_bottom + 20), f"HOST {hostname}", font=font_small, fill=FG)
    draw.text((8, box_bottom + 34), f"TIME {now}", font=font_small, fill=FG)
    draw.text((8, box_bottom + 48), batt_str, font=font_small, fill=FG)

    epd.display(epd.getbuffer(image))
    time.sleep(2)
    epd.sleep()


if __name__ == "__main__":
    main()
