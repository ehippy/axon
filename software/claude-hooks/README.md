# Axon for Claude Code

A native Claude plugin with `.claude-plugin/plugin.json`, lifecycle hooks, and the
`axon-activity` skill. SessionStart/UserPromptSubmit supply the reporting command
for the current session, even when many chats use the same working directory.
Goals and results are explicit agent reports; no prompts, transcripts, command
arguments, filenames from tool input, or replies are scraped.

Use [the plugin manager](../plugins/README.md) to build a portable package or merge
hooks + the skill into existing user settings. `report_status.py` remains a
compatible entry point. Machine status is aggregated from the same safe fields
for older Axon displays. Restart/resume Claude after changing hook definitions.
