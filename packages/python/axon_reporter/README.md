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

Each report also carries `machine_kind` (`laptop`, `desktop`, or `server`) for the
browser UI's host icon.

Claude Code hooks also fetch dashboard input from the journal server at
`AXON_SERVER` (default `http://<AXON_BROKER>:8510`). `AXON_LISTEN_SECONDS`
(default 10800, `0` disables) bounds how long an idle session listens for input. It is detected from the battery on macOS and from DMI or
devicetree chassis type, then a system battery, on Linux. Hosts that cannot be
classified (for example a Raspberry Pi) send none; set `AXON_MACHINE_KIND` there.

## Account resources

`axon-resources codex` starts the signed-in native Codex app-server, calls only
`initialize` and `account/rateLimits/read`, then exits. It starts no inference turn.
Run it once a minute on one signed-in machine per account. `AXON_CODEX_BIN` can
select the CLI executable. Its auth remains with Codex.

For Claude Code Pro/Max, set `statusLine.command` to
`/path/to/venv/bin/axon-resources claude-statusline`. Claude supplies documented
`rate_limits.five_hour` and `rate_limits.seven_day` JSON after its first API response.
The collector also prints a small quota status line. To preserve an existing
renderer, append `--display-command 'your existing command'`; it receives the same
stdin and its stdout is passed through. Missing quota fields leave the last good
sample intact. Identical callbacks publish at most once a minute; changed limits
publish immediately. Keep status-line output local; only whitelisted windows,
account alias and observation time enter the retry outbox.

The existing broker settings in `~/.config/axon/env` apply. `AXON_CODEX_ACCOUNT`
and `AXON_CLAUDE_ACCOUNT` label distinct accounts; both default to `primary` (or
`AXON_RESOURCE_ACCOUNT`). Machines using the same alias report the same account,
not additive usage. The dashboard displays the latest observation, with stale
and expired readings labeled explicitly. Choose different aliases for different
subscriptions. The collector can live in a separate reporter virtualenv so normal
agent-plugin syncs do not remove it.

For macOS, a user LaunchAgent with `StartInterval=60`, `RunAtLoad=true` and
`ProgramArguments=["/path/to/venv/bin/axon-resources", "codex"] keeps Codex fresh.
Use a PATH containing your Codex installation, or set `AXON_CODEX_BIN`. On Linux,
a user systemd timer can invoke the same command. Claude readings update through
its status-line callback while it is in use; inactive accounts can become stale.
