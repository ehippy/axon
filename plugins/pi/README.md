# Axon activity plugin for Pi

In-process adapter for the [Pi coding agent](https://pi.dev) (`pi`).
`index.ts` subscribes to Pi's lifecycle events and forwards identifiers and
event names to the shared Python reporter (`hooks/run.py`); raw prompts, tool
input, and transcripts are never read. The per-session reporter command is
appended to the system prompt on user-initiated runs.

Event mapping: `session_start` → SessionStart, `before_agent_start` (user
runs) → UserPromptSubmit, `tool_call` → PreToolUse, `tool_execution_end` →
PostToolUse, `ui_prompt_start` → PermissionRequest, `agent_settled` →
Stop/Interrupt, `session_shutdown` (except reload) → SessionEnd.

Layout: Pi's native extension directory format — `package.json` with a
`pi.extensions` manifest pointing at `index.ts`. Pi loads it through jiti, so
no build step or `node_modules` is needed; `@earendil-works/pi-coding-agent`
and node types resolve inside Pi.

Install with the managed installer (symlinks this package into
`~/.pi/agent/extensions/axon-activity` and the skill into
`~/.pi/agent/skills/axon-activity`), or copy/symlink the built package into
that directory yourself. Restart Pi (or `/reload`) afterwards.

Reporter settings come from the Pi process environment: `AXON_BROKER`,
`AXON_PORT`, `AXON_MACHINE`, `AXON_SOURCE`, `AXON_ACTIVITY_HOME`, and
optionally `AXON_PYTHON` (defaults to `python3`).

Pitfall: launching Pi from a terminal that already exports `AXON_SOURCE`
(for example a Hermes or Claude Code session with Axon reporting) makes the
Pi session report under the parent agent source. Start Pi from a clean shell,
or unset `AXON_SOURCE` for it, so it falls back to `pi-<installation-id>`.
