"""Screen renderers for the Axon display daemon. Each function draws onto a
canvas made by hud.new_canvas() given the daemon's shared state dict. Screens
that show up in the double-tap cycle are listed in CYCLE, in display order.
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
        return f"BATT {b['percent']}%{' CHG' if b['charging'] else ''}", b
    except OSError:
        return "BATT -- (offline)", None


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


def _cycle_footer(draw, box_bottom):
    if state_screen_index() is not None:
        idx, total = state_screen_index()
        hud.page_dots(draw, hud.W - 10 - total * 10, box_bottom + 44, total, idx)


# Set by the daemon before each render so footers can draw page dots -
# simplest way to avoid threading state through every render call.
_current_index = [None, None]


def state_screen_index():
    if _current_index[0] is None:
        return None
    return tuple(_current_index)


def set_screen_index(idx, total):
    _current_index[0], _current_index[1] = idx, total


def render_boot(image, draw, state):
    y, _ = hud.header_block(image, draw, "AXON", "SYSTEM BOOT")
    hud.double_rule(draw, y + 6)
    box_top, box_bottom = y + 16, 190
    draw.text((18, box_top + 40), "ONLINE", font=hud.font_title, fill=hud.FG)
    hud.double_rule(draw, box_bottom + 12)
    draw.text((8, box_bottom + 20), "axon display", font=hud.font_small, fill=hud.FG)
    draw.text((8, box_bottom + 34), "daemon started", font=hud.font_small, fill=hud.FG)


def render_idle(image, draw, state):
    y, x = hud.header_block(image, draw, "AXON")

    hostname = state.get("hostname", "?")
    draw.text((x, 8), hostname, font=hud.font_tiny, fill=hud.FG)
    hb = draw.textbbox((x, 8), hostname, font=hud.font_tiny)
    draw.text((hb[2] + 8, 8), _wifi_ssid(), font=hud.font_tiny, fill=hud.FG)

    _, batt = _battery_line()
    pct = batt["percent"] if batt else 0
    charging = batt["charging"] if batt else False
    icon_w = 14
    hud.battery_icon(draw, hud.W - 4 - icon_w - 2, 8, pct, charging=charging, w=icon_w, h=7)

    hud.double_rule(draw, y + 6)

    box_top, box_bottom = y + 16, 190
    hud.tick_ruler(draw, hud.W - 14, box_top + 4, box_bottom - 4)

    agent_state = state.get("agent_state", "idle").upper()
    draw.text((18, box_top + 10), "STATUS", font=hud.font_small, fill=hud.FG)

    # Cap art height to what's actually left for it, computed from the real
    # box bounds rather than a guessed constant - leaves room below for the
    # task line (~20px) regardless of how tall header_block's vertical title
    # ends up being.
    art_max_h = max(20, box_bottom - (box_top + 24) - 20)
    art = hud.load_art("idle", max_size=(70, art_max_h))
    if art:
        draw.bitmap((18, box_top + 24), art, fill=hud.FG)
        task_y = box_top + 24 + art.size[1] + 6
    else:
        draw.text((18, box_top + 24), agent_state, font=hud.font_title, fill=hud.FG)
        hud.signal_bars(draw, 18, box_top + 58)
        task_y = box_top + 78

    if agent_state == "RUNNING" and state.get("agent_task"):
        task = state["agent_task"][:16]
        draw.text((18, task_y), task, font=hud.font_tiny, fill=hud.FG)

    hud.double_rule(draw, box_bottom + 12)


def _compact_uptime():
    raw = _run(["cat", "/proc/uptime"])
    try:
        secs = int(float(raw.split()[0]))
    except (ValueError, IndexError):
        return "?"
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def render_diagnostics(image, draw, state):
    y, _ = hud.header_block(image, draw, "AXON", "DIAGNOSTICS // 02")
    hud.double_rule(draw, y + 6)

    box_top, box_bottom = y + 16, 190

    ts_ip = _run(["/usr/bin/remote-networking", "ip", "-4"])
    uptime = _compact_uptime()  # not uptime -p: its verbose phrasing ("5
    # hours, 21 minutes") ran off the right edge of the panel
    loadavg = _run(["cat", "/proc/loadavg"])
    load = loadavg.split(" ")[0] if loadavg != "?" else "?"

    lines = [("network", ts_ip), ("UPTIME", uptime), ("LOAD 1M", load)]
    ly = box_top + 8
    for label, value in lines:
        draw.text((18, ly), label, font=hud.font_small, fill=hud.FG)
        draw.text((18, ly + 13), value, font=hud.font_body, fill=hud.FG)
        ly += 30

    hud.double_rule(draw, box_bottom + 12)
    _cycle_footer(draw, box_bottom)


def render_battery(image, draw, state):
    y, _ = hud.header_block(image, draw, "AXON", "POWER // 03")
    hud.double_rule(draw, y + 6)

    box_top, box_bottom = y + 16, 190

    _, batt = _battery_line()
    if batt:
        draw.text((18, box_top + 14), f"{batt['percent']}%", font=hud.font_title, fill=hud.FG)
        bar_w, bar_h = 70, 14
        bx, by = 18, box_top + 44
        draw.rectangle((bx, by, bx + bar_w, by + bar_h), outline=hud.FG)
        fill_w = int(bar_w * batt["percent"] / 100)
        draw.rectangle((bx, by, bx + fill_w, by + bar_h), fill=hud.FG)
        draw.text((18, box_top + 66), f"{batt['voltage']:.2f}V", font=hud.font_body, fill=hud.FG)
        draw.text((18, box_top + 84), "CHARGING" if batt["charging"] else "ON BATTERY", font=hud.font_small, fill=hud.FG)
    else:
        draw.text((18, box_top + 30), "PISUGAR", font=hud.font_body, fill=hud.FG)
        draw.text((18, box_top + 46), "OFFLINE", font=hud.font_body, fill=hud.FG)

    hud.double_rule(draw, box_bottom + 12)
    _cycle_footer(draw, box_bottom)


def render_network(image, draw, state):
    y, _ = hud.header_block(image, draw, "AXON", "NETWORK // 04")
    hud.double_rule(draw, y + 6)

    box_top, box_bottom = y + 16, 190

    ssid = _wifi_ssid()
    local_ip = _run(["hostname", "-I"]).split(" ")[0]
    ts_ip = _run(["/usr/bin/remote-networking", "ip", "-4"])

    lines = [("WIFI", ssid), ("LOCAL IP", local_ip), ("network IP", ts_ip)]
    ly = box_top + 8
    for label, value in lines:
        draw.text((18, ly), label, font=hud.font_small, fill=hud.FG)
        draw.text((18, ly + 13), value, font=hud.font_body, fill=hud.FG)
        ly += 30

    hud.double_rule(draw, box_bottom + 12)
    _cycle_footer(draw, box_bottom)


def render_agents(image, draw, state):
    y, _ = hud.header_block(image, draw, "AXON", "AGENTS // 05")
    hud.double_rule(draw, y + 6)

    box_top, box_bottom = y + 16, 190

    agents = state.get("agents", {})
    if not agents:
        draw.text((18, box_top + 30), "NO AGENTS", font=hud.font_body, fill=hud.FG)
        draw.text((18, box_top + 46), "REPORTING", font=hud.font_body, fill=hud.FG)
    else:
        # most recently updated first, capped to what fits the box
        ordered = sorted(agents.items(), key=lambda kv: kv[1]["ts"], reverse=True)
        ly = box_top + 4
        for source, info in ordered[:5]:
            draw.text((18, ly), source[:14].upper(), font=hud.font_small, fill=hud.FG)
            age = _relative_time(info["ts"])
            age_w = draw.textbbox((0, 0), age, font=hud.font_tiny)[2]
            draw.text((hud.W - 8 - age_w, ly), age, font=hud.font_tiny, fill=hud.FG)
            task = info.get("task") or info.get("state", "?")
            draw.text((18, ly + 12), task[:18], font=hud.font_tiny, fill=hud.FG)
            ly += 26

    hud.double_rule(draw, box_bottom + 12)
    _cycle_footer(draw, box_bottom)


def render_shutdown(image, draw, state):
    y, _ = hud.header_block(image, draw, "AXON", "SHUTDOWN // 00")
    hud.double_rule(draw, y + 6)
    box_top, box_bottom = y + 16, 190
    draw.text((18, box_top + 30), "POWERING", font=hud.font_title, fill=hud.FG)
    draw.text((18, box_top + 56), "DOWN", font=hud.font_title, fill=hud.FG)
    hud.double_rule(draw, box_bottom + 12)
    draw.text((8, box_bottom + 20), "LONG-PRESS", font=hud.font_small, fill=hud.FG)
    draw.text((8, box_bottom + 34), "TRIGGERED", font=hud.font_small, fill=hud.FG)


# Screens single-tap always returns to.
HOME = "idle"

# Screens double-tap cycles through, in order. Each render_* function may call
# _cycle_footer to draw page dots showing position in this list.
CYCLE = ["diagnostics", "battery", "network", "agents"]

RENDERERS = {
    "boot": render_boot,
    "idle": render_idle,
    "agents": render_agents,
    "diagnostics": render_diagnostics,
    "battery": render_battery,
    "network": render_network,
    "shutdown": render_shutdown,
}
