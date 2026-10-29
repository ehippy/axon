# Axon wire contracts

Application release: **0.3.0**. Activity event and session-view version: **1**.
Breaking source packaging does not change the meaning of activity events.

| Topic/interface | Producer | Consumer | Delivery |
| --- | --- | --- | --- |
| `axon/activity/<source>/<session>/events` | Reporter | Server | QoS 1, not retained |
| `axon/activity/view` | Server | Handset | QoS 1, retained, versioned JSON |
| `axon/integrations/<source>/status` | Glass/Gabbie/other adapters | Handset | Retained JSON with sender timestamp |
| `axon/handsets/<device-id>/ptt` | Voice-enabled handset | Gabbie adapter | QoS 1, nonretained `start` / `stop` bytes |
| HTTP `127.0.0.1:8510` | Server | Query client | `/health`, `/sessions`, `/events`, session input |
| HTTP `127.0.0.1:8500` | Handset | Local controls | `/health`, POST `/event`, GET `/event/<gesture>` |
| TCP `127.0.0.1:8765` | Gabbie | Voice-enabled handset | Newline-delimited JSON voice events |

## Activity events and projection

The envelope contains `version`, `source`, `session`, `id`, `at` (Unix seconds),
`kind`, and a `data` object. Source/session/event identifiers contain 1–128 ASCII
letters, digits, dots, underscores, or hyphens. Topic identity must match the
payload identity. Future timestamps beyond 60 seconds are rejected.

Kinds: `start`, `progress`, `heartbeat`, `waiting`, `blocked`, `finished`,
`stopped`, `interrupted`. `start` clears the previous goal/step/result; progress
patches existing fields. A stop hook means waiting, never successful completion.
Finished requires a nonempty outcome and 1–12 nonempty evidence strings, each at
most 500 characters. Lease seconds must be an integer between 30 and 3600
(default 900). Optional `machine_kind` is `laptop`, `desktop`, or `server`;
servers older than this field reject events that carry it, so upgrade the server
before reporters. Optional `listening_until` is a Unix time until which an idle
session accepts dashboard input (`0` when it stops listening). Field limits and exact validation live in the dependency-free
[Python contract](../packages/python/axon_protocol/axon_protocol/__init__.py).

States are working, waiting, blocked, finished, stopped, interrupted. Duplicate
IDs are ignored; older arrivals remain history without replacing newer current
state. Finished survives subsequent lifecycle hooks until a new start. Expired
nonterminal sessions project as `lost_contact`, without implying success/failure.

The retained view is `{ "version": 1, "sessions": [...] }`. Rows carry source,
session, current semantic fields, state, effective_state, fresh, lease_seconds,
and updated_at. Terminal rows remain queryable; the display view includes only
the last 24 hours. The handset recomputes stale active leases on replay and
rejects unsupported view versions. See [fixtures](fixtures/) and contract tests.

### Rolling out new fields

Hosts and the server update separately, so either may be a release ahead.

- A server accepts `data` keys it does not know if they are plainly named (`[a-z][a-z0-9_]{0,39}`) and together serialize to at most 2 KB of JSON. It keeps them in the stored event but never merges them into session state or the published view. Known fields stay strictly validated.
- Resource snapshots work the same way: unknown top-level keys, metrics and GPU readings are dropped within the same budget, not stored.
- `GET /health` advertises `protocol: {version, data_fields}`. Reporters cache it for an hour (five minutes after a failed check) and leave out fields the server does not list. A server too old to advertise gets events unfiltered, as before.
- Reporters publish only what their own validator returns, so a field no version understands never leaves the host.

## Integration status and voice

Integration status requires `state` (`idle`, `running`, `error`) and a positive
Unix-seconds `ts`; `task` is optional. Arrival time is never substituted for a
missing timestamp. Integration keys are namespaced separately from session rows.
Integration status is display-only and is not session history.

Each voice handset has a deployment-selected device ID and PTT topic. Consumers
must subscribe to that exact topic; there is no global PTT broadcast. Events use
one persistent publisher connection to preserve press/release ordering.
Gabbie JSON-lines events contain `event`; `thinking`, `speaking`, and `confirming`
show voice feedback, `done_speaking` returns to idle. Ambient listening or
transcribing events do not take over the display. Button-down feedback is local.

## Read API and local controls

Server queries support source/session/project/state filters, text search, and
event pagination (`after`, `limit`, `next_after`), with optional since/until.
The query client exposes these filters. Defaults and complete response examples
can be exercised with `axon-query --json sessions` or `axon-query --json events`.

Handset POST `/event` accepts `{"button":"single"}`; valid gestures are single,
double, long. The GET form is retained for button controllers that cannot quote
JSON. Long press invokes local shutdown; protect access to this control API.
The former no-op POST `/status` endpoint is removed.

### Browser timeline queries

`/events` defaults to its original ascending sequence order. Add `order=desc`
for the newest reported events; the response includes `next_before`. Pass it as
`before` for the next older page. The original `after` cursor remains available
for ascending consumers; `next_after` is the highest sequence in the returned
page. Optional `agent`, `project`, `q`, `since`, `until`, and `exclude_kind`
filters support the browser timeline. Project/agent filters refer to the current
session association. Text search treats its query literally. Sequence order is
arrival order; event timestamps retain the publisher's reported time.

### Session input

`POST /sessions/<source>/<session>/messages` with `{"text": "..."}` (1–4000
characters) and `Authorization: Bearer <token>` queues input for an existing
session: 201 on success, 403 when the server has no input token, 401 for a wrong
token, 404 for an unknown session. `GET` on the same path lists the session's
messages with `delivered_at` (null while queued) and `input` (whether sending is
enabled). Hooks call `POST /sessions/<source>/<session>/inbox/claim?wait=<s>`
(long-poll, at most 55 s): it atomically marks pending messages delivered and
returns them. Messages follow the journal retention period.

Dashboard archiving is server metadata, separate from work state. Authenticated
`POST /sessions/<source>/<session>/archive` with `{"archived": true}` archives;
`false` restores. It uses the same bearer token as session input. Archived rows
carry `archived_at` in `/sessions`; event history is unchanged. Optional boolean
`data.user_turn` marks an actual user prompt, not agent progress. A fresh marker
after archiving restores the session; stale delivery and heartbeat do not.

Session API rows include `activity_at`: the latest retained non-heartbeat event
time, or the first retained heartbeat for heartbeat-only sessions. It separates
meaningful activity ranking from `updated_at` lease freshness.

## Account quota snapshots

`axon/resources/<provider>/<account>` carries a version-1 snapshot with exact fields
`version`, `provider` (`codex` or `claude`), `account` (a configured safe alias),
`observed_at` (epoch seconds), and `windows`. Each window has `name` (`short` or
`weekly`), `window_minutes`, `used_percent` and `resets_at`. Window values may be
null when unavailable; zero usage is a valid reading. Percentages must be finite
and within 0..100. Unknown fields are rejected, including credential/account-detail
fields. Collectors normalize only these fields from native provider responses.

A newer observation replaces the previous one for that provider/account; duplicate
or older snapshots do not overwrite it. Snapshots do not create activity sessions
or event history, and are never added across hosts. `GET /resources` includes
unknown standing entries for Codex/Claude until their first observation.

## Git project identity

New reporters add `project_id` (64 lowercase hexadecimal characters), `worktree`
(up to 120 characters), and optional `topic` (up to 400 characters). Empty project
fields clear a previous association when a session moves outside git. Old events
retain their string `project`; no stored history is rewritten.

The reporter hashes the normalized origin URL, or sorted root commits when there
is no origin. Worktrees use the main checkout's name. The origin, checkout paths,
and native agent session IDs remain in the host-local registry; events contain
only public project metadata and Axon's hashed session identity. Explicit
`--project` labels that differ from the detected repository name become topics.
An unborn repository without an origin has no stable project ID until its first
commit. Adding/changing an origin changes its identity; the project merge API
planned in #21 will handle that migration without rewriting historical events.
