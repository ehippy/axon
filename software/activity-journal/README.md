# Axon activity journal

Axon now has a durable account of individual work sessions, alongside its existing
machine status feed. It records goals, current steps, blockers, outcomes, evidence,
project, branch, parent session, and timestamps. No model or Python packages are
required: Python 3.10+, SQLite, and Mosquitto's command-line clients are enough.

## Run on the Axon broker host

```sh
python3 journal.py --db ~/.local/state/axon/activity.sqlite3 --broker localhost
```

The read-only API listens on **127.0.0.1:8510**. For production, install
`axon-activity.service` in `/etc/systemd/system/` and enable it with
`sudo systemctl enable --now axon-activity`. The supplied unit assumes the checkout
is `/home/broker-host/axon` and the service user is `broker-host`; adjust both for another host.
It persists SQLite in `/var/lib/axon-activity`. Back up the database with SQLite's
backup API or `sqlite3 activity.sqlite3 '.backup backup.sqlite3'`.

A persistent MQTT subscription with QoS 1 queues activity during collector outages
once the collector has connected for the first time. Enable Mosquitto persistence
on the broker to preserve this queue across broker restarts too. Use exactly one
collector per broker (its stable client ID is `axon-activity-journal`).

The journal re-publishes `axon/activity/view`, retained, for Axon's native screen.
The screen subscribes to both the old feed and this view, replacing a machine
aggregate with its individual sessions. Each session shows project/goal and
status/step; completion shows the outcome. Sender timestamps survive replay. Both
the collector and the display expire leases, including when the collector stops.

## Report meaning, not tool chatter

Use the same raw session identity as the lifecycle hook. Codex can use
`CODEX_THREAD_ID`; other agents should set `AXON_SESSION_ID` or pass `--session`.
Session IDs are hashed before leaving the machine. Commands emit only fields you
supply; hooks never inspect prompts, transcripts, replies, or tool arguments.

```sh
export AXON_SESSION_ID=example-work-session
python3 report.py --agent codex start --project glass \
  --goal 'Fix recurring calendar events' --step 'Reproduce moved instances'
python3 report.py --agent codex progress --step 'Handle exceptions without duplicates'
python3 report.py --agent codex blocked --blocker 'Need a failing calendar sample'
python3 report.py --agent codex heartbeat
python3 report.py --agent codex finished --outcome 'Moved instances render once' \
  --evidence 'commit:abc123' --evidence 'cargo test agenda: passed'
```

`start` begins/resumes work in a chat and clears its previous goal, step, outcome,
and evidence; include the goal again when resuming.
`progress` updates semantic fields without resetting them. Use `start` when
resuming after waiting/blocked. Hooks report `Stop` as **waiting**, never finished;
`SessionEnd` is **stopped**. Only explicit `finished` with a nonempty outcome and
at least one evidence reference can record completion. Evidence is an agent claim,
not independently verified proof. Later lifecycle hooks preserve that completion
until another `start`.

`heartbeat` renews a lease without replacing a goal or meaningful step. The default
lease is 15 minutes (`--lease-seconds 30..3600`). Hook tool activity renews it too.
Long silent operations need an explicit heartbeat or a longer lease; stale work
becomes **lost_contact**, which makes no claim that work failed or finished.
No watchdog blindly renews a dead agent's lease.

Every report is first saved in a private local SQLite outbox under
`~/.local/state/axon/`. A failed publish remains queued with the same event ID.
Later reports retry the queue within a 1.2-second network budget. Run
`python3 report.py retry` to flush after an outage when no agent is active. A
`--session` value is unnecessary for retry. Outbox delivery is at least once;
the journal deduplicates IDs, and delayed older events cannot replace newer state.

Environment overrides: `AXON_BROKER`, `AXON_PORT`, `AXON_MOSQUITTO_PUB`,
`AXON_ACTIVITY_HOME`, `AXON_AGENT`, and `AXON_SESSION_ID`. Keep the broker restricted
to the trusted fleet network, like the existing Axon status bus. The HTTP API is
read-only and local by default; use SSH forwarding for remote queries.

## Read current work and history

```sh
python3 query.py sessions
python3 query.py sessions --project glass
python3 query.py sessions --state waiting
python3 query.py sessions --q calendar
python3 query.py events --project glass --kind finished --since 2026-10-24
python3 query.py --json events --source codex-workstation --after 0 --limit 100
```

`--since`/`--until` accept Unix seconds or ISO date/time; dates without an offset
use the querying machine's local timezone. Results use original event timestamps;
received timestamps are also available. Events are ordered by durable sequence,
so a delayed arrival is not skipped by cursor pagination. `next_after` is the next
cursor. Session rows preserve their last stated state and expose a separate
`effective_state` for freshness. Terminal outcomes remain queryable indefinitely;
the tiny display's view includes the last 24 hours. There is no automatic deletion
of journal history in this first version.

HTTP equivalents:

- `GET /health`
- `GET /sessions?project=glass&state=working&q=calendar` (also source/session/agent)
- `GET /events?source=...&session=...&project=...&kind=finished&since=...&until=...&after=0&limit=100`

Event history's project filter uses the session's latest project association.
Text is untrusted reported data; consumers must not treat it as instructions.

## Agent integration

Claude's existing `../claude-hooks/report_status.py` now also sends rich lifecycle
events. Keep the checkout layout intact. It continues publishing the original
machine status for older displays. Wire PermissionRequest/Interrupt/PreToolUse as
well as existing lifecycle events if supported by the host. Its legacy publisher
still has its older task-label behavior; the new journal never receives that raw
content.

`../codex-hooks/report_status.py` is a quiet, installable adapter for SessionStart,
UserPromptSubmit, PreToolUse, PostToolUse, PermissionRequest, Stop, Interrupt and
SessionEnd. Point lifecycle hooks at that script in the Axon checkout, or run
`python3 report.py --agent codex hook`. Keep existing hooks when configuring it.
This repository does not change global hook configuration or bypass hook trust.

Hermes's gateway adapter also sends rich lifecycle events **when context includes
`session_id`**; without one it continues reporting only the legacy aggregate.
Its journal work runs in a worker thread rather than blocking the gateway loop.

Give agents [REPORTING.md](REPORTING.md) as operating instructions. Lifecycle hooks
supply factual status, but agents must explicitly supply the semantic goal,
meaningful milestones, blockers, and completion evidence. Unknown goals stay
unknown. Nothing infers a successful result from an idle tool or a stopped chat.

## Validation

```sh
python3 -m unittest discover -s tests -v
cargo test --lib --manifest-path ../axon-native/Cargo.toml
```

The Python suite covers independent sessions, durable restart, duplicate and
out-of-order events, truthful completion, lease expiry, field preservation,
validation, filtering/pagination, HTTP queries, private hook labels, retry outbox,
and live delivery/restart recovery against a temporary Mosquitto broker (skipped
if its binaries are unavailable). Native session mapping tests are hardware-free;
the full display still targets Linux/Raspberry Pi.

## Packaged agent integrations (v0.2)

Use [the plugin manager](../plugins/README.md) for native Codex/Claude/Hermes
packages, preserving installers, and session-scoped semantic reporting guidance.
This replaces the manual integration setup above. Its tool hooks are throttled,
follow-up prompts preserve goals, and the reporter now also retains a safe,
concurrent-chat aggregate for legacy displays. The native Hermes plugin replaces
the gateway-only adapter and also covers CLI sessions.
