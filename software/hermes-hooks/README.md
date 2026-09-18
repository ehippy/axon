# Hermes status hooks

`axon-status/` is a Hermes **gateway hook** publishing Hermes's activity to
broker-host's own Mosquitto broker, so it shows up on Axon's AGENTS screen as
source `hermes`. See `docs/PROTOTYPE-PWNAGOTCHI.md` for the full story and
the MQTT contract (`axon/agents/<source>/status`, JSON `{"state", "task"}`).

Hermes ([hermes-agent.nousresearch.com](https://hermes-agent.nousresearch.com/docs/))
is its own independent agent harness - not built on Claude Code, and its
hooks are a completely separate mechanism from `software/claude-hooks/`.
It has four native hook systems (gateway hooks, plugin hooks, shell hooks,
outbound webhooks); this uses gateway hooks since they can run arbitrary
Python directly, no separate receiver needed.

## Install (already done on broker-host)

```
mkdir -p ~/.hermes/hooks/axon-status
cp axon-status/HOOK.yaml axon-status/handler.py ~/.hermes/hooks/axon-status/
hermes gateway restart   # gateway hooks load at startup - check for
                          # "N hook(s) loaded" in `hermes logs`
```

## Testing without a live gateway event

`hermes hooks test <event>` does **not** cover gateway hooks - it only
covers shell/plugin hooks, which use a different event vocabulary
(`pre_tool_call`, `on_session_start`, etc., not `agent:start` etc.). To test
a gateway hook's handler directly:

```
cd ~/.hermes/hooks/axon-status
python3 -c "
import asyncio, sys
sys.path.insert(0, '.')
import handler
asyncio.run(handler.handle('agent:start', {'message': 'test'}))
"
```

then check display-device's AGENTS screen or `sudo journalctl -u axon-display` on
display-device for `agent update: hermes -> ...`.

## Why asyncio, not subprocess.run()

Gateway hooks run **in-process** inside the Hermes gateway (unlike Claude
Code hooks, which are separate subprocess invocations per event) - a
blocking `subprocess.run()` here would stall the gateway's own event loop,
not just this one hook. `handler.py` uses
`asyncio.create_subprocess_exec` + `asyncio.wait_for` instead.
