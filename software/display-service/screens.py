"""Screen renderers for the Axon display daemon. Each function draws onto a
canvas made by hud.new_canvas() given the daemon's shared state dict. Screens
that show up in the double-tap cycle are listed in CYCLE, in display order.
"""
import subprocess

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
    y = hud.header_block(image, draw, "AXON", "SYSTEM BOOT")
    hud.double_rule(draw, y + 6)
    box_top, box_bottom = y + 16, 190
    draw.text((18, box_top + 40), "ONLINE", font=hud.font_title, fill=hud.FG)
    hud.double_rule(draw, box_bottom + 12)
    draw.text((8, box_bottom + 20), "axon display", font=hud.font_small, fill=hud.FG)
    draw.text((8, box_bottom + 34), "daemon started", font=hud.font_small, fill=hud.FG)


def render_idle(image, draw, state):
    y = hud.header_block(image, draw, "AXON", state.get("hostname", "?"))
    hud.double_rule(draw, y + 6)

    box_top, box_bottom = y + 16, 190
    hud.tick_ruler(draw, hud.W - 14, box_top + 4, box_bottom - 4)

    agent_state = state.get("agent_state", "idle").upper()
    draw.text((18, box_top + 10), "STATUS", font=hud.font_small, fill=hud.FG)

    art = hud.load_art("idle", max_size=(70, 70))
    if art:
        draw.bitmap((18, box_top + 24), art, fill=hud.FG)
    else:
        draw.text((18, box_top + 24), agent_state, font=hud.font_title, fill=hud.FG)
        hud.signal_bars(draw, 18, box_top + 58)

    if agent_state == "RUNNING" and state.get("agent_task"):
        task = state["agent_task"][:16]
        draw.text((18, box_top + 78), task, font=hud.font_tiny, fill=hud.FG)

    hud.double_rule(draw, box_bottom + 12)

    battery_str, _ = _battery_line()
    draw.text((8, box_bottom + 20), battery_str, font=hud.font_small, fill=hud.FG)


def render_diagnostics(image, draw, state):
    y = hud.header_block(image, draw, "AXON", "DIAGNOSTICS // 02")
    hud.double_rule(draw, y + 6)

    box_top, box_bottom = y + 16, 190

    ts_ip = _run(["/usr/bin/remote-networking", "ip", "-4"])
    uptime = _run(["uptime", "-p"]).replace("up ", "")
    loadavg = _run(["cat", "/proc/loadavg"])
    load = loadavg.split(" ")[0] if loadavg != "?" else "?"

    lines = [("network", ts_ip), ("UPTIME", uptime), ("LOAD 1M", load)]
    ly = box_top + 14
    for label, value in lines:
        draw.text((18, ly), label, font=hud.font_small, fill=hud.FG)
        draw.text((18, ly + 13), value, font=hud.font_body, fill=hud.FG)
        ly += 34

    hud.double_rule(draw, box_bottom + 12)
    _cycle_footer(draw, box_bottom)


def render_battery(image, draw, state):
    y = hud.header_block(image, draw, "AXON", "POWER // 03")
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
    y = hud.header_block(image, draw, "AXON", "NETWORK // 04")
    hud.double_rule(draw, y + 6)

    box_top, box_bottom = y + 16, 190

    ssid = _run(["/usr/bin/nmcli", "-t", "-f", "active,ssid", "dev", "wifi"])
    ssid = next((l.split(":", 1)[1] for l in ssid.splitlines() if l.startswith("yes:")), "?")
    local_ip = _run(["hostname", "-I"]).split(" ")[0]
    ts_ip = _run(["/usr/bin/remote-networking", "ip", "-4"])

    lines = [("WIFI", ssid), ("LOCAL IP", local_ip), ("network IP", ts_ip)]
    ly = box_top + 14
    for label, value in lines:
        draw.text((18, ly), label, font=hud.font_small, fill=hud.FG)
        draw.text((18, ly + 13), value, font=hud.font_body, fill=hud.FG)
        ly += 34

    hud.double_rule(draw, box_bottom + 12)
    _cycle_footer(draw, box_bottom)


def render_shutdown(image, draw, state):
    y = hud.header_block(image, draw, "AXON", "SHUTDOWN // 00")
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
CYCLE = ["diagnostics", "battery", "network"]

RENDERERS = {
    "boot": render_boot,
    "idle": render_idle,
    "diagnostics": render_diagnostics,
    "battery": render_battery,
    "network": render_network,
    "shutdown": render_shutdown,
}
