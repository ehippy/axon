# Axon reporter

Standalone event creation, validation, private outbox/retry, and agent lifecycle
helpers. Depends on `axon-protocol`, never on `axon-server`.

```sh
python3 -m venv .venv-reporter
.venv-reporter/bin/pip install ./packages/python/axon_protocol ./packages/python/axon_reporter
export AXON_BROKER=localhost AXON_PORT=1883
.venv-reporter/bin/axon-report --agent codex --session example-chat start --project glass --goal 'Check status'
.venv-reporter/bin/axon-report --agent codex --session example-chat finished --outcome 'Status verified' --evidence 'test: status check'
```

Use `start` to begin/resume work, `progress` for milestones, and waiting/blocked
when attention is needed. Only explicit finished reports with outcome and
evidence represent completion. Stop hooks leave waiting; teardown is stopped.
Heartbeats renew leases and never infer success. Hook failures must not stop work.

`AXON_ACTIVITY_HOME` chooses private local state (default `~/.local/state/axon`).
`identity.sqlite3` stores a random stable installation label; `outbox.sqlite3`
queues events and hook heartbeat timestamps. Existing outbox rows can be retried;
old aggregate tables, if present, are ignored. `axon-report retry` drains queued
events. A later report retries too; there is no autonomous retry daemon.

`AXON_MACHINE` sets an explicit installation label and `AXON_SOURCE` overrides
agent-plus-installation source identity. Defaults never read the hostname. Use
unique labels per installation. Labels contain letters/digits/dots/underscores/
hyphens. Session IDs are hashed, but source labels/project/branch/explicit semantic
fields are visible to broker/server readers. Keep secrets, raw transcripts, and
personal details out of those fields. Do not copy identity state to independent
installations unless you deliberately want them to share an identity.

`AXON_BROKER`, `AXON_PORT`, and `AXON_MOSQUITTO_PUB` configure the publisher.
The transport is plain MQTT; secure the surrounding deployment as described in
[architecture](../../../docs/architecture.md). There is no aggregate machine
status publisher in this release: plugins emit activity events only.

Reporters also read literal `KEY=value` or `export KEY=value` settings from
`~/.config/axon/env` (or `AXON_CONFIG_FILE`). Environment variables take precedence.
Set `AXON_BROKER`, `AXON_PORT`, and an explicit friendly `AXON_MACHINE` there.
The file is read for each report, so label changes need no restart. Changing the
machine label preserves the generated source identity. Hostnames are never inferred.
