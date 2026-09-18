"""Screen renderers for the Axon display daemon. Each function draws onto a
canvas made by hud.new_canvas() given the daemon's shared state dict.

One real screen (idle) plus two transient ones (boot, shutdown) - see the
project history for why (portrait e-ink was cramped, went landscape; then
the multi-screen cycle got cut for being more complexity than it was
worth; now on the Whisplay's much roomier 240x280 color LCD, back to
portrait since 240px is plenty wide on its own).
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
        return get_battery_status()
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
    draw.text((next_x, 40), "ONLINE", font=hud.font_title, fill=hud.FG)
    draw.text((next_x, 75), "axon display", font=hud.font_small, fill=hud.FG)
    draw.text((next_x, 92), "daemon started", font=hud.font_small, fill=hud.FG)


def render_idle(image, draw, state):
    next_x = hud.header_block(image, draw, "AXON")

    hostname = state.get("hostname", "?")
    draw.text((next_x, 6), hostname, font=hud.font_tiny, fill=hud.FG)
    hb = draw.textbbox((next_x, 6), hostname, font=hud.font_tiny)
    draw.text((hb[2] + 8, 6), _wifi_ssid(), font=hud.font_tiny, fill=hud.FG)

    batt = _battery_line()
    pct = batt["percent"] if batt else 0
    charging = batt["charging"] if batt else False
    icon_w = 18
    hud.battery_icon(draw, hud.W - 6 - icon_w - 3, 5, pct, charging=charging, w=icon_w, h=9)

    hud.double_rule(draw, 24, x0=next_x)

    agents = state.get("agents", {})
    row_top = 34
    if not agents:
        draw.text((next_x, row_top + 40), "NO AGENTS", font=hud.font_body, fill=hud.FG)
        draw.text((next_x, row_top + 60), "REPORTING", font=hud.font_body, fill=hud.FG)
    else:
        ordered = sorted(agents.items(), key=lambda kv: kv[1]["ts"], reverse=True)
        row_h = 36
        max_rows = (hud.H - row_top - 6) // row_h
        ly = row_top
        for source, info in ordered[:max_rows]:
            draw.text((next_x, ly), source.upper(), font=hud.font_body, fill=hud.FG)
            age = _relative_time(info["ts"])
            age_w = draw.textbbox((0, 0), age, font=hud.font_tiny)[2]
            draw.text((hud.W - 8 - age_w, ly + 3), age, font=hud.font_tiny, fill=hud.FG)
            task = info.get("task") or info.get("state", "?")
            draw.text((next_x, ly + 18), task[:36], font=hud.font_tiny, fill=hud.FG)
            ly += row_h


def render_shutdown(image, draw, state):
    next_x = hud.header_block(image, draw, "AXON")
    draw.text((next_x, 30), "POWERING", font=hud.font_title, fill=hud.FG)
    draw.text((next_x, 55), "DOWN", font=hud.font_title, fill=hud.FG)
    draw.text((next_x, 90), "long-press triggered", font=hud.font_small, fill=hud.FG)


# Screen single-tap always returns to.
HOME = "idle"

# No page-cycling (single screen) - kept as an empty list rather than
# removed so daemon.py's double-tap handling doesn't need a special case.
CYCLE = []

RENDERERS = {
    "boot": render_boot,
    "idle": render_idle,
    "shutdown": render_shutdown,
}
