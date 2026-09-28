"""Hermes gateway hook: publishes Hermes's activity to broker-host's own Mosquitto
broker (localhost - the broker lives on the same box) so it shows up on
Axon's AGENTS screen alongside Claude Code sessions from other machines. See
docs/PROTOTYPE-PWNAGOTCHI.md in the axon repo for the MQTT contract.

Uses "hermes" as the source name (topic axon/agents/hermes/status), not
"broker-host" - broker-host the machine might someday also report its own interactive
Claude Code activity separately, and conflating the two would be confusing
on the AGENTS screen.

Runs in-process inside the Hermes gateway (unlike the Claude Code hooks,
which are separate subprocess invocations), so this uses asyncio subprocess
calls rather than a blocking subprocess.run() - a blocking call here would
stall the gateway's event loop, not just this one hook.
"""
import asyncio
import json

MQTT_BROKER = "localhost"


def _truncate(text, length):
    text = (text or "").strip()
    if len(text) <= length:
        return text
    cut = text[:length].rsplit(" ", 1)[0]
    return (cut or text[:length]) + "…"


async def _publish(state, task):
    topic = "axon/agents/hermes/status"
    payload = json.dumps({"state": state, "task": task})
    try:
        proc = await asyncio.create_subprocess_exec(
            "mosquitto_pub", "-h", MQTT_BROKER, "-t", topic, "-m", payload,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=3)
    except Exception:
        pass  # never let a status ping take down the gateway


async def handle(event_type: str, context: dict):
    if event_type == "session:start":
        await _publish("idle", "session started")
    elif event_type == "agent:start":
        await _publish("running", _truncate(context.get("message"), 40))
    elif event_type == "agent:step":
        tools = context.get("tool_names") or []
        task = ", ".join(tools) if tools else "thinking"
        await _publish("running", _truncate(task, 30))
    elif event_type == "agent:end":
        await _publish("idle", "awaiting input")
    elif event_type == "session:end":
        await _publish("idle", "session ended")
