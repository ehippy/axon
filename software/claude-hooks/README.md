# Claude Code status hooks

`report_status.py` publishes this machine's Claude Code activity to broker-host's
Mosquitto broker, which shows up on Axon's AGENTS screen. See
`docs/PROTOTYPE-PWNAGOTCHI.md` for the full story and the MQTT contract
(`axon/agents/<source>/status`, JSON `{"state", "task"}`).

## Set up on a new machine

1. Install an MQTT publish client:
   - Arch/CachyOS: `sudo pacman -S mosquitto`
   - Debian/Ubuntu: `sudo apt install mosquitto-clients`
   - macOS: `brew install mosquitto`
2. Clone/pull this repo onto that machine.
3. Add a `hooks` block to `~/.claude/settings.json` (global - applies to all
   projects on that machine) wiring `SessionStart`, `UserPromptSubmit`,
   `Stop`, and `SessionEnd` to:
   ```
   python3 -u /path/to/boxen/software/claude-hooks/report_status.py
   ```
   (same command for all four - it branches on `hook_event_name` from stdin).
   See `~/.claude/settings.json` on workstation for a working example.
4. Test the script standalone before trusting the hooks:
   ```
   echo '{"hook_event_name":"UserPromptSubmit","prompt":"test"}' | python3 report_status.py
   ```
   then check display-device's AGENTS screen or `sudo journalctl -u axon-display` on
   display-device for `agent update: <hostname> -> ...`.

Hook changes take effect on the *next* Claude Code session on that machine,
not retroactively in one already running.

## Why mosquitto_pub, not a Python MQTT library

This needs to run on whatever OS Claude Code happens to be on (Linux and
macOS so far). A native package-manager binary is far less fragile across
machines than juggling pip/venv and PEP 668's externally-managed-environment
restrictions per OS just to publish one small message.
