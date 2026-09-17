#!/usr/bin/env python3
"""Shutdown screen + action for display-device's e-Paper HUD. Bound to the PiSugar
button's long-press gesture. Draws a confirmation screen, then actually
powers the box off - runs as root via pisugar-server's button_shell, so no
sudo needed. See hud.py for shared drawing primitives.
"""
import os
import time

import hud


def main():
    image, draw = hud.new_canvas()

    hud.outer_frame(draw)
    y = hud.header_block(draw, "BOXEN", "SHUTDOWN // 03")
    hud.double_rule(draw, y + 6)

    box_top, box_bottom = y + 16, 190
    hud.bracket_box(draw, 10, box_top, hud.W - 10, box_bottom)

    draw.text((18, box_top + 30), "POWERING", font=hud.font_title, fill=hud.FG)
    draw.text((18, box_top + 56), "DOWN", font=hud.font_title, fill=hud.FG)

    hud.double_rule(draw, box_bottom + 12)
    draw.text((8, box_bottom + 20), "LONG-PRESS", font=hud.font_small, fill=hud.FG)
    draw.text((8, box_bottom + 34), "TRIGGERED", font=hud.font_small, fill=hud.FG)

    hud.show(image, hold=1)
    time.sleep(1)
    os.system("/sbin/shutdown -h now")


if __name__ == "__main__":
    main()
