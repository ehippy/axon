# Axon activity plugin for opencode

In-process adapter for [opencode](https://opencode.ai). `plugin/axon.js` is a
dual-shape plugin — the v2 `export default { id, setup }` definition and a v1
`server()` hook factory (supported from OpenCode 1.18.29) — so one file works
on both generations. It forwards identifiers and event names to the shared
Python reporter (`hooks/run.py`); raw prompts, tool arguments, and
transcripts are never read. The per-session reporter command is appended to
the system prompt (one entry per session, so the prompt cache stays warm).

v2 hook mapping: `session.hook("prompt")` → SessionStart (first turn) +
UserPromptSubmit, `session.hook("context")` → system-prompt injection,
`tool.hook("execute.before"/"after")` → PreToolUse/PostToolUse heartbeats,
`permission.hook("evaluate")` → PermissionRequest (observation only; the
gate decision is untouched), `session.idle` → Stop, `session.error` →
Interrupt, setup cleanup → SessionEnd for the sessions this instance
observed. The v1 `server()` surface maps `chat.message`,
`experimental.chat.system.transform`, `tool.execute.*`, `permission.ask`,
events, and `dispose` equivalently.

Layout: a plain JS module auto-loaded from `~/.config/opencode/plugins/`.
No build step, no node_modules, no type imports.

Install with the managed installer (symlinks the plugin file into
`~/.config/opencode/plugins/axon-activity.js` and the skill into
`~/.config/opencode/skills/axon-activity` — the symlink matters because the
plugin resolves its own realpath to find `hooks/run.py`). Restart opencode
afterwards.

Reporter settings come from the opencode process environment: `AXON_BROKER`,
`AXON_PORT`, `AXON_MACHINE`, `AXON_SOURCE`, `AXON_ACTIVITY_HOME`, and
optionally `AXON_PYTHON` (defaults to `python3`).

Pitfall: launching opencode from a terminal that already exports
`AXON_SOURCE` (for example a Hermes or Claude Code session with Axon
reporting) makes the opencode session report under the parent agent source.
Start opencode from a clean shell, or unset `AXON_SOURCE` for it, so it
falls back to `opencode-<installation-id>`.
