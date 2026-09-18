#!/usr/bin/env python3
"""Claude Code hook: publishes this machine's session status to broker-host's
Mosquitto broker so it shows up on Axon's AGENTS screen (see
docs/PROTOTYPE-PWNAGOTCHI.md for the MQTT contract).

One script handles all wired-up hook events - it branches on
hook_event_name from the hook's stdin JSON payload, so every event
registers the same command in .claude/settings.json rather than needing a
near-identical script per event.

PostToolUse is the important one for making this representative of actual
work: it reports what tool just ran (editing a file, running a command,
searching, etc.) rather than a static string or 40 raw characters of
whatever the user happened to type. Lifecycle events (start/stop/end) get
a bit of personality instead of flat "session started"/"session ended" -
picked randomly from a small phrase pool for variety, in the same terse
sci-fi voice as the HUD itself (AGENT UNIT, SYSTEM BOOT, etc).

Uses the mosquitto_pub CLI rather than a Python MQTT library on purpose:
this hook needs to run on whatever machine Claude Code is on (Linux or
macOS so far), and a native binary from the OS's own package manager is far
less fragile across machines than juggling pip/venv and PEP 668 restrictions
per-OS just to publish one small message.

Never raises past main() and always has a short timeout - a status ping
must not be able to hang or break an actual Claude Code session.
"""
import json
import os
import random
import socket
import subprocess
import sys

MQTT_BROKER = "localhost"

SESSION_START_PHRASES = ["spinning up", "waking up", "AXON online", "booting in"]
STOP_PHRASES = ["standing by", "awaiting input", "idle, thinking", "holding position"]
SESSION_END_PHRASES = ["signing off", "powering down", "going dark", "logging off"]


def _truncate(text, length):
    text = (text or "").strip()
    if len(text) <= length:
        return text
    cut = text[:length].rsplit(" ", 1)[0]
    return (cut or text[:length]) + "…"


def _format_tool(tool_name, tool_input):
    tool_input = tool_input or {}
    if tool_name in ("Edit", "Write", "NotebookEdit"):
        path = tool_input.get("file_path", "")
        return f"editing {os.path.basename(path)}" if path else f"using {tool_name}"
    if tool_name == "Read":
        path = tool_input.get("file_path", "")
        return f"reading {os.path.basename(path)}" if path else "reading"
    if tool_name == "Bash":
        return f"running: {_truncate(tool_input.get('command'), 30)}"
    if tool_name in ("Grep", "Glob"):
        return f"searching: {_truncate(tool_input.get('pattern'), 24)}"
    if tool_name in ("WebSearch", "WebFetch"):
        return f"researching: {_truncate(tool_input.get('query') or tool_input.get('url'), 24)}"
    if tool_name == "Task":
        desc = tool_input.get("description", "")
        return f"delegating: {_truncate(desc, 24)}" if desc else "spawning an agent"
    return f"using {tool_name}"


def publish(state, task):
    source = socket.gethostname().lower()
    topic = f"axon/agents/{source}/status"
    payload = json.dumps({"state": state, "task": task})
    try:
        subprocess.run(
            ["mosquitto_pub", "-h", MQTT_BROKER, "-t", topic, "-m", payload],
            timeout=3,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass  # broker-host unreachable, mosquitto_pub missing, whatever - never block the session


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        data = {}

    event = data.get("hook_event_name", "")

    if event == "SessionStart":
        publish("idle", random.choice(SESSION_START_PHRASES))
    elif event == "UserPromptSubmit":
        publish("running", _truncate(data.get("prompt"), 40))
    elif event == "PostToolUse":
        publish("running", _format_tool(data.get("tool_name"), data.get("tool_input")))
    elif event == "Stop":
        publish("idle", random.choice(STOP_PHRASES))
    elif event == "SessionEnd":
        publish("idle", random.choice(SESSION_END_PHRASES))


if __name__ == "__main__":
    main()
