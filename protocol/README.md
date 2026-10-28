# Axon wire contracts

Application release: **0.3.0**. Activity event and session-view version: **1**.
Breaking source packaging does not change the meaning of activity events.

| Topic/interface | Producer | Consumer | Delivery |
| --- | --- | --- | --- |
| `axon/activity/<source>/<session>/events` | Reporter | Server | QoS 1, not retained |
| `axon/activity/view` | Server | Handset | QoS 1, retained, versioned JSON |
| `axon/integrations/<source>/status` | Glass/Gabbie/other adapters | Handset | Retained JSON with sender timestamp |
| `axon/handsets/<device-id>/ptt` | Voice-enabled handset | Gabbie adapter | QoS 1, nonretained `start` / `stop` bytes |
| HTTP `127.0.0.1:8510` | Server | Query client | Read-only `/health`, `/sessions`, `/events` |
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
before reporters. Field limits and exact validation live in the dependency-free
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
