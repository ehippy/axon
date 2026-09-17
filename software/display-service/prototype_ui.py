#!/usr/bin/env python3
"""Status/idle screen for display-device's e-Paper HUD. Bound to the PiSugar button's
single-tap gesture ("refresh status") - see button_diagnostics.py and
button_shutdown.py for the other two gestures, and hud.py for shared drawing
primitives.
"""
import os
from datetime import datetime

import hud
from pisugar_client import get_status as get_battery_status


def main():
    image, draw = hud.new_canvas()

    hud.outer_frame(draw)
    y = hud.header_block(draw, "BOXEN", "AGENT UNIT // 01")
    hud.double_rule(draw, y + 6)

    box_top, box_bottom = y + 16, 190
    hud.bracket_box(draw, 10, box_top, hud.W - 10, box_bottom)
    hud.tick_ruler(draw, hud.W - 14, box_top + 4, box_bottom - 4)

    draw.text((18, box_top + 10), "STATUS", font=hud.font_small, fill=hud.FG)
    draw.text((18, box_top + 24), "IDLE", font=hud.font_title, fill=hud.FG)
    hud.signal_bars(draw, 18, box_top + 58)

    hud.double_rule(draw, box_bottom + 12)

    hostname = os.uname()[1]
    now = datetime.now().strftime("%H:%M:%S")
    try:
        batt = get_battery_status()
        batt_str = f"BATT {batt['percent']}%{' CHG' if batt['charging'] else ''}"
    except OSError:
        batt_str = "BATT --%  (pisugar-server unreachable)"
    draw.text((8, box_bottom + 20), f"HOST {hostname}", font=hud.font_small, fill=hud.FG)
    draw.text((8, box_bottom + 34), f"TIME {now}", font=hud.font_small, fill=hud.FG)
    draw.text((8, box_bottom + 48), batt_str, font=hud.font_small, fill=hud.FG)

    hud.show(image)


if __name__ == "__main__":
    main()
