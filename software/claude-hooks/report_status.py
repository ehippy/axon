#!/usr/bin/env python3
"""Claude Code hook: publishes this machine's session status to broker-host's
Mosquitto broker so it shows up on Axon's AGENTS screen (see
docs/PROTOTYPE-PWNAGOTCHI.md for the MQTT contract).

One script handles all four wired-up hook events - it branches on
hook_event_name from the hook's stdin JSON payload, so the same command can
be registered under SessionStart, UserPromptSubmit, Stop, and SessionEnd in
.claude/settings.json rather than needing four near-identical scripts.

Uses the mosquitto_pub CLI rather than a Python MQTT library on purpose:
this hook needs to run on whatever machine Claude Code is on (Linux or
macOS so far), and a native binary from the OS's own package manager is far
less fragile across machines than juggling pip/venv and PEP 668 restrictions
per-OS just to publish one small message.

Never raises past main() and always has a short timeout - a status ping
must not be able to hang or break an actual Claude Code session.
"""
import json
import socket
import subprocess
import sys

MQTT_BROKER = "localhost"


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
        publish("idle", "session started")
    elif event == "UserPromptSubmit":
        prompt = (data.get("prompt") or "").strip().replace("\n", " ")
        publish("running", prompt[:40])
    elif event == "Stop":
        publish("idle", "")
    elif event == "SessionEnd":
        publish("idle", "session ended")


if __name__ == "__main__":
    main()
