"""Screen renderers for the Axon display daemon. Each function draws onto a
canvas made by hud.new_canvas() given the daemon's shared state dict.

Down to one real screen (idle) plus two transient ones (boot, shutdown) -
the earlier double-tap cycle through separate diagnostics/battery/network/
agents screens got cut for being more complexity than it was worth. idle
now folds host/wifi/battery into a compact top strip and gives the rest of
the (now-landscape) panel to the agents list, which was the actually-useful
part of the old agents screen anyway.
"""
import subprocess
import time

import hud
from pisugar_client import get_status as get_battery_status


def _run(cmd, default="?"):
    try:
        return subprocess.check_output(cmd, timeout=3).decode().strip()
    except Exception:
        return default


def _battery_line():
    try:
        b = get_battery_status()
        return b
    except OSError:
        return None


def _wifi_ssid():
    out = _run(["/usr/bin/nmcli", "-t", "-f", "active,ssid", "dev", "wifi"])
    return next((l.split(":", 1)[1] for l in out.splitlines() if l.startswith("yes:")), "?")


def _relative_time(ts):
    delta = time.time() - ts
    if delta < 60:
        return "now"
    if delta < 3600:
        return f"{int(delta // 60)}m"
    if delta < 86400:
        return f"{int(delta // 3600)}h"
    return f"{int(delta // 86400)}d"


def render_boot(image, draw, state):
    next_x = hud.header_block(image, draw, "AXON")
    draw.text((next_x, 30), "ONLINE", font=hud.font_title, fill=hud.FG)
    draw.text((next_x, 60), "axon display daemon started", font=hud.font_small, fill=hud.FG)


def render_idle(image, draw, state):
    next_x = hud.header_block(image, draw, "AXON")

    hostname = state.get("hostname", "?")
    draw.text((next_x, 4), hostname, font=hud.font_tiny, fill=hud.FG)
    hb = draw.textbbox((next_x, 4), hostname, font=hud.font_tiny)
    draw.text((hb[2] + 8, 4), _wifi_ssid(), font=hud.font_tiny, fill=hud.FG)

    batt = _battery_line()
    pct = batt["percent"] if batt else 0
    charging = batt["charging"] if batt else False
    icon_w = 14
    hud.battery_icon(draw, hud.W - 4 - icon_w - 2, 4, pct, charging=charging, w=icon_w, h=7)

    hud.double_rule(draw, 18, x0=next_x)

    agents = state.get("agents", {})
    row_top = 26
    if not agents:
        draw.text((next_x, row_top + 20), "NO AGENTS REPORTING", font=hud.font_body, fill=hud.FG)
    else:
        ordered = sorted(agents.items(), key=lambda kv: kv[1]["ts"], reverse=True)
        row_h = 30
        max_rows = (hud.H - row_top - 4) // row_h
        ly = row_top
        for source, info in ordered[:max_rows]:
            draw.text((next_x, ly), source.upper(), font=hud.font_body, fill=hud.FG)
            age = _relative_time(info["ts"])
            age_w = draw.textbbox((0, 0), age, font=hud.font_tiny)[2]
            draw.text((hud.W - 8 - age_w, ly + 2), age, font=hud.font_tiny, fill=hud.FG)
            task = info.get("task") or info.get("state", "?")
            draw.text((next_x, ly + 15), task[:40], font=hud.font_tiny, fill=hud.FG)
            ly += row_h


def render_shutdown(image, draw, state):
    next_x = hud.header_block(image, draw, "AXON")
    draw.text((next_x, 20), "POWERING DOWN", font=hud.font_title, fill=hud.FG)
    draw.text((next_x, 55), "long-press triggered", font=hud.font_small, fill=hud.FG)


# Screens single-tap always returns to.
HOME = "idle"

# No page-cycling anymore - kept as an empty list rather than removed so
# daemon.py's double-tap handling doesn't need a special case for "no cycle
# at all", just an empty one.
CYCLE = []

RENDERERS = {
    "boot": render_boot,
    "idle": render_idle,
    "shutdown": render_shutdown,
}
