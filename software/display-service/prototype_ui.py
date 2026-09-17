#!/usr/bin/env python3
"""One-off prototype UI for the Waveshare 2.13" e-Paper HAT (V3) on display-device.
Not the real display service yet — just a static status-face mockup to prove
out the look before wiring it to live agent/battery state.
"""
import os
import sys
import time
from datetime import datetime

sys.path.append(os.path.expanduser("~/e-Paper/RaspberryPi_JetsonNano/python/lib"))
from waveshare_epd import epd2in13_V3
from PIL import Image, ImageDraw, ImageFont

FONT_PATH = os.path.expanduser("~/e-Paper/RaspberryPi_JetsonNano/python/pic/Font.ttc")
font_title = ImageFont.truetype(FONT_PATH, 22)
font_body = ImageFont.truetype(FONT_PATH, 14)
font_small = ImageFont.truetype(FONT_PATH, 11)

W, H = 250, 122


def draw_sleepy_face(draw, cx, cy):
    # two closed/sleepy eyes as downward arcs
    draw.arc((cx - 20, cy - 8, cx, cy + 12), start=200, end=340, fill=0, width=3)
    draw.arc((cx + 16, cy - 8, cx + 36, cy + 12), start=200, end=340, fill=0, width=3)
    # curved smile
    draw.arc((cx - 10, cy + 4, cx + 26, cy + 24), start=20, end=160, fill=0, width=2)
    # drifting zzz's
    draw.text((cx + 42, cy - 28), "z", font=font_small, fill=0)
    draw.text((cx + 54, cy - 38), "z", font=font_small, fill=0)
    draw.text((cx + 65, cy - 47), "z", font=font_small, fill=0)


def draw_wifi_icon(draw, x, y):
    for i, r in enumerate((10, 7, 4)):
        draw.arc((x - r, y - r, x + r, y + r), start=225, end=315, fill=0, width=2)
    draw.ellipse((x - 1, y + 3, x + 1, y + 5), fill=0)


def draw_battery_icon(draw, x, y, pct):
    w, h = 26, 12
    draw.rectangle((x, y, x + w, y + h), outline=0)
    draw.rectangle((x + w, y + 3, x + w + 3, y + h - 3), outline=0, fill=0)
    fill_w = int((w - 4) * (pct / 100.0))
    draw.rectangle((x + 2, y + 2, x + 2 + fill_w, y + h - 2), fill=0)


def main():
    epd = epd2in13_V3.EPD()
    epd.init()
    epd.Clear(0xFF)

    image = Image.new("1", (W, H), 255)
    draw = ImageDraw.Draw(image)

    draw.rectangle((0, 0, W - 1, H - 1), outline=0)

    draw.text((8, 4), "boxen", font=font_title, fill=0)
    draw.line((0, 30, W, 30), fill=0)

    draw_sleepy_face(draw, 40, 75)

    draw.text((110, 38), "status: idle", font=font_body, fill=0)
    hostname = os.uname()[1]
    draw.text((110, 56), f"host: {hostname}", font=font_body, fill=0)
    draw.text((110, 74), "battery: --%  (not wired yet)", font=font_small, fill=0)

    draw.line((0, H - 20, W, H - 20), fill=0)
    now = datetime.now().strftime("%H:%M:%S")
    draw.text((6, H - 17), now, font=font_small, fill=0)
    draw_wifi_icon(draw, W - 55, H - 13)
    draw_battery_icon(draw, W - 40, H - 16, 75)

    epd.display(epd.getbuffer(image))
    time.sleep(2)
    epd.sleep()


if __name__ == "__main__":
    main()
