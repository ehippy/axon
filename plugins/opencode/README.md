# Axon activity plugin for opencode

In-process adapter for [opencode](https://opencode.ai). `plugin/axon.js`
subscribes to opencode's plugin hooks and forwards identifiers and event
names to the shared Python reporter (`hooks/run.py`); raw prompts, tool
arguments, and transcripts are never read. The per-session reporter command
is appended to the system prompt via the experimental chat system transform
(one entry per session, so the prompt cache stays warm).

Hook mapping: `chat.message` → SessionStart (first turn) + UserPromptSubmit,
`tool.execute.before`/`after` → PreToolUse/PostToolUse heartbeats,
`permission.ask` → PermissionRequest (observation only; the gate decision is
untouched), `session.idle` → Stop, `session.error` → Interrupt, plugin
`dispose` → SessionEnd for the sessions this instance observed.

Layout: a plain JS file auto-loaded from `~/.config/opencode/plugin/`.
No build step, no node_modules, no type imports.

Install with the managed installer (symlinks the plugin file into
`~/.config/opencode/plugin/axon-activity.js` and the skill into
`~/.config/opencode/skill/axon-activity`), or copy the file there yourself.
Restart opencode afterwards.

Reporter settings come from the opencode process environment: `AXON_BROKER`,
`AXON_PORT`, `AXON_MACHINE`, `AXON_SOURCE`, `AXON_ACTIVITY_HOME`, and
optionally `AXON_PYTHON` (defaults to `python3`).

Pitfall: launching opencode from a terminal that already exports
`AXON_SOURCE` (for example a Hermes or Claude Code session with Axon
reporting) makes the opencode session report under the parent agent source.
Start opencode from a clean shell, or unset `AXON_SOURCE` for it, so it
falls back to `opencode-<installation-id>`.
