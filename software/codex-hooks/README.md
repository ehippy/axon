# Axon for Codex

Native plugin metadata, lifecycle hooks, and the `axon-activity` reporting skill.
SessionStart/UserPromptSubmit supply a command scoped to the current chat; tools
supply throttled heartbeats. Goals, milestones, blockers and results are written
explicitly by the agent. Hooks never inspect prompt/reply text or tool arguments.

Build a self-contained native package or install hooks + the skill into existing
Codex user settings using [the plugin manager](../plugins/README.md). The old
`report_status.py` entry point now runs the same integration. Review/trust the
updated hooks in Codex after installation; no trust state is changed by scripts.
