# Reporting work to Axon

When configured with this Axon reporter, publish a concise goal when work starts,
then update only for a meaningful milestone, change of direction, blocker, or
result. Keep one stable session identity per chat and use the same identity as its
lifecycle hook; child work gets its own identity and `--parent` reference.

Use `python3 /path/to/axon/software/activity-journal/report.py` with `--agent` and
`--session` (or AXON_SESSION_ID / CODEX_THREAD_ID), followed by:

- `start --project PROJECT --goal 'Desired outcome' --step 'Current meaningful step'`
- `progress --step 'What the work is now resolving'`
- `waiting --step 'Decision needed from the user'`
- `blocked --blocker 'Specific dependency preventing progress'`
- `finished --outcome 'Concrete result' --evidence 'commit/PR/test/artifact reference'`

Do not transmit credentials, raw prompts, transcripts, or command arguments.
Summarize work in plain language. Do not call something finished unless the
requested outcome has been achieved. If work merely stops, leave it waiting,
interrupted, or stopped. Evidence references describe what was actually checked;
they are not a substitute for checking it.

Use `heartbeat` during long silent work, and `start` when resuming a blocked or
waiting session. A reporting failure must not stop the actual task; its durable
outbox can be retried later using `report.py retry`.
