#!/usr/bin/env python3
"""Diagnostics screen for display-device's e-Paper HUD. Bound to the PiSugar button's
double-tap gesture. See hud.py for shared drawing primitives.
"""
import subprocess

import hud


def _run(cmd, default="?"):
    try:
        return subprocess.check_output(cmd, timeout=3).decode().strip()
    except Exception:
        return default


def main():
    image, draw = hud.new_canvas()

    hud.outer_frame(draw)
    y = hud.header_block(draw, "BOXEN", "DIAGNOSTICS // 02")
    hud.double_rule(draw, y + 6)

    box_top, box_bottom = y + 16, 190
    hud.bracket_box(draw, 10, box_top, hud.W - 10, box_bottom)
    hud.tick_ruler(draw, hud.W - 14, box_top + 4, box_bottom - 4)

    ts_ip = _run(["/usr/bin/remote-networking", "ip", "-4"])
    uptime = _run(["uptime", "-p"]).replace("up ", "")
    load = _run(["cat", "/proc/loadavg"]).split(" ")[0] if _run(["cat", "/proc/loadavg"]) != "?" else "?"

    lines = [
        ("network", ts_ip),
        ("UPTIME", uptime),
        ("LOAD 1M", load),
    ]
    ly = box_top + 14
    for label, value in lines:
        draw.text((18, ly), label, font=hud.font_small, fill=hud.FG)
        draw.text((18, ly + 13), value, font=hud.font_body, fill=hud.FG)
        ly += 34

    hud.double_rule(draw, box_bottom + 12)
    draw.text((8, box_bottom + 20), "DOUBLE-TAP", font=hud.font_small, fill=hud.FG)
    draw.text((8, box_bottom + 34), "REFRESHED", font=hud.font_small, fill=hud.FG)

    hud.show(image)


if __name__ == "__main__":
    main()
