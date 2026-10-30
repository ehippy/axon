# Install the server

Requires Python 3.10+ and Mosquitto's `mosquitto_pub` / `mosquitto_sub` commands.
Run a broker separately with persistence enabled. One collector per broker.

From a source checkout:

```sh
python3 -m venv .venv-server
.venv-server/bin/pip install ./packages/python/axon_protocol ./server
.venv-server/bin/axon-server --db /path/to/private/activity.sqlite3 --broker localhost
```

For a wheel release, install only the server and its protocol dependency:

```sh
python3 -m venv .venv-server
.venv-server/bin/pip install --no-index --find-links /path/to/release/wheels axon-server
```

No reporter, handset, or plugin installation is needed on this host.
The read API defaults to `127.0.0.1:8510`. Set broker host and port with
`AXON_BROKER` / `AXON_PORT`, or `--broker` / `--broker-port`; CLI flags win.
Use `--listen`, `--port`, `--db`, `--retention-days` (default 30; 0 disables
retention), and `--no-mqtt` as needed. Database paths belong in local deployment
configuration. The service unit explicitly chooses a private state directory.

For systemd, create a dedicated `axon` account and install the server virtualenv
under `/opt/axon/server`. Copy [axon-server.service](../server/deploy/axon-server.service)
to `/etc/systemd/system/`. Put deployment values in `/etc/axon/server.env`:

```ini
AXON_BROKER=localhost
AXON_PORT=1883
```

Then reload systemd and enable `axon-server`. This starts the service: do it only
when replacing or setting up the intended deployment. The unit creates
`/var/lib/axon-activity` with restrictive permissions. Adjust paths/account in a
local override if your installation differs.

The schema is unchanged from the prior journal. Stop the old collector before
starting this one against the same database/broker. Back up SQLite with its
backup API (or stop the service before copying); preserve the original and
validate a copy before switching. Retention applies on startup.

## Automatic updates

A host that runs the server as a systemd user service can follow `main` on its own.
[axon_update.py](../server/deploy/axon_update.py) keeps each server commit in
`~/.local/share/axon/server/releases/<version>-<sha>/venv` with `current` pointing
at the live one, which is what the user unit's `ExecStart` runs. Every five minutes
it fetches the branch and, for a commit that changes the server or protocol package:

1. waits until every GitHub check on that commit passed (a failed commit is never deployed);
2. builds a fresh release virtualenv and backs up the database with SQLite's backup API;
3. switches `current`, restarts `axon-server` and waits for `/health`;
4. switches back to the previous release if the server does not answer, and skips that commit.

Commits that touch neither are recorded without a restart. It keeps the five newest
releases and database backups, and each deploy installs the updater's own newest copy.

```sh
mkdir -p ~/.local/share/axon/updater ~/.config/systemd/user
cp server/deploy/axon_update.py ~/.local/share/axon/updater/
cp server/deploy/axon-update.service server/deploy/axon-update.timer ~/.config/systemd/user/
loginctl enable-linger "$USER"
systemctl --user daemon-reload && systemctl --user enable --now axon-update.timer
```

### Requesting a deploy

Start the server with `--updater ~/.local/share/axon/server` (or `AXON_UPDATER_ROOT`)
to expose the updater over the API. `GET /update` shows the deployed, previous and
refused commits and whether a request is pending. `POST /update` needs the input
token and answers `202` at once; the updater then checks every 45 seconds for up to
20 minutes and deploys as soon as CI passes, instead of waiting for the next timer
tick. Requests within 30 seconds of each other share one run, and only one updater
runs at a time, so at most a few dozen GitHub requests follow a burst of requests.

```sh
curl -X POST -H "Authorization: Bearer $(cat ~/.config/axon/input-token)" http://127.0.0.1:8510/update
```

Override defaults (`AXON_UPDATE_BRANCH`, `AXON_UPDATE_KEEP`, `AXON_UPDATE_HEALTH_URL`, ...)
in `~/.config/axon/update.env`. `journalctl --user -u axon-update` shows each decision;
`update-state.json` beside `current` records the deployed and any failed commit. Every
push to the tracked branch reaches the host once CI passes, so keep it to reviewed work.

## Query client

```sh
python3 -m venv .venv-query
.venv-query/bin/pip install ./clients/query
.venv-query/bin/axon-query --url http://127.0.0.1:8510 --json sessions
```

The client requires neither server nor reporter Python modules. Remote access to
the loopback API should use a secured tunnel; reads have no authentication.

## Browser UI

Open `http://127.0.0.1:8510/` on the server, or forward that loopback port over
SSH and open the forwarded local address. The packaged UI offers Now,
a filterable event timeline, and session details with evidence. The board groups
live reporting sessions by host, then agent, with a separate tile for each
source/session pair. Tile order is stable across activity updates. Agent identity
colors follow the handset palette; status dots separately indicate work state.
Host labels come from reported `machine` values (`AXON_MACHINE` sets a friendly
label, `auto` reports the host name); missing host metadata is shown explicitly
instead of inferred. A laptop, desktop,
or server icon from [Bootstrap Icons](https://icons.getbootstrap.com/) (MIT, vendored
in `web/icons`) marks each host's reported `machine_kind`. It polls every
five seconds; paging into older timeline events pauses refresh until you click
Refresh. Heartbeats are hidden by default. Session links encode source and
session in the URL fragment, so dashboards can link directly to a session.

`--no-web` disables browser assets while preserving the JSON API. No CDN, frontend
build step, or extra service is needed. The UI uses the server's existing listen
address and authentication model: keep it on loopback or behind authenticated
access, since activity can contain personal project details. Agent-reported text
is displayed as text; evidence becomes a link only for explicit HTTP(S) URLs.

The top bar's time horizon controls both board sessions (by last report) and
timeline events. It defaults to one hour and offers 15m, 30m, 1h, 6h, 24h, 7d,
30d, and Any time. Selecting a longer horizon reveals older sessions without
changing their reported state or extending their heartbeat lease.

Within each host, live agent groups appear first, then groups with the most
recent meaningful activity. Sessions inside each group follow the same rule.
Heartbeat renewals update freshness without changing this ordering. Activity is
ranked in one-minute buckets with deterministic ties to avoid noisy reshuffling.

Hosts occupy side-by-side board columns when space permits. Agent groups and
session tiles stack vertically inside each host; narrow screens stack hosts.

## Session input

The session detail view can send messages into running Claude Code, Codex and Hermes sessions.
Input is disabled until the server has a token, because a message makes an agent
act with that user's permissions:

```sh
umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > ~/.config/axon/input-token
axon-server ... --input-token-file ~/.config/axon/input-token
```

`AXON_INPUT_TOKEN_FILE` works too. Paste the token into the session view once; the
browser keeps it in local storage. Sending requires `Authorization: Bearer <token>`.
Hooks claim queued messages without the token. Anyone who can reach the API could
therefore claim (and so swallow) a queued message, but never inject one; keep the
server on a trusted network.

Delivery depends on the session's state:

- **Working:** the next tool call's hook adds the message to Claude's context.
- **Idle:** after each turn, a background `asyncRewake` Stop hook listens for up to
  three hours (`AXON_LISTEN_SECONDS`, at most 14000) and wakes Claude immediately when
  a message arrives. Waiting costs no tokens, and the terminal stays usable: typing
  locally ends that listen window. The board shows *Listening* with the time left.
- **Otherwise** the message stays queued until the session next runs a hook.

Codex gets the same in-turn delivery through its `PostToolUse` and `UserPromptSubmit`
hooks. Its hooks cannot start a turn, so there is no idle listener. Instead, its Stop
hook claims anything that arrived after the last tool call and returns
`decision: "block"`, so Codex answers it before the turn ends. A message sent to an
idle Codex thread waits for its next turn, and the board says so.

Hermes takes input in-process (before each model call and after tool results) and has
no idle listener either; input queued while it is idle arrives when it next runs.

## Archive sessions

Archive on a card hides that session from the board across browsers and restarts.
Undo restores it immediately; the collapsed Archived section also offers Restore.
Archiving uses the same server input token as messaging, and does not alter the
agent's reported state or delete its event history. Archived sessions remain in
the timeline and session API, identified by `archived_at`.

Background reports, heartbeat renewals, and replays leave the archive in place.
An updated reporter's next `UserPromptSubmit` emits a `user_turn: true` marker;
a fresh marker after the archive restores the session automatically. Update the
installed agent plugins alongside the server to enable this behavior.

The browser detects code/UI deployments from the server's `X-Axon-Version`
fingerprint and reloads automatically on a changed version. It checks when the
live stream reconnects, when a tab becomes visible, and periodically as a fallback.
The URL preserves the view and filters; unsent card/detail messages are carried
across the reload in that tab's session storage. An in-flight send finishes first.

## Account quota resources

The standing Codex and Claude panels show remaining short-window and weekly
allowance, reset countdowns and observation age. They are account-level snapshots,
independent of board filters and sessions. A missing window is Unknown, a passed
reset is Awaiting update, and observations older than five minutes are Stale.
Credentials remain on the collecting machine.

Install the reporter wheel and configure its
[account collectors](../packages/python/axon_reporter/README.md#account-resources).
The server's MQTT collector subscribes to `axon/resources/+/+`; `/resources` serves
the latest durable snapshot per provider/account and resource changes signal SSE.
There is no additional browser polling service or provider login on the server.

## Token totals

The Codex and Claude panels show tokens sent up and received over the top bar's
time horizon, summed across every host and session. The Claude and Codex hooks read
running totals from the session's own transcript (`transcript_path`), keeping only
usage numbers and resuming where the last read stopped, and attach them to lifecycle
reports as `tokens: {input, cached, output}`. Sent-up counts include cached context,
which dominates agent sessions; hover the line for the cached share. A session already
running when the period starts counts from its first report inside the period. Claude
subagent transcripts are separate files and are not yet counted. The totals come from
`GET /resources/tokens?since=<epoch seconds>`. Hosts start sending counts once
their plugins are updated.

## Shared inference servers

An inference server already reports its own load, so the server can simply poll it.
Name each server with a stable label and repeat the option:

```sh
axon-server --inference neuralforge=http://neuralforge:13308/metrics \
  --inference moar=http://moar:13308/metrics --inference-interval 10
```

Each label gets its own panel showing running and queued requests, generation and
prompt speed and KV-cache use. Another inference server is another option, not
another integration. Nothing is installed on the inference host and it publishes
nothing; this reads the metrics endpoint it already exposes.

**Polling cannot show hardware.** GUFO and llama.cpp report requests, speed, KV cache
and confirmed GPU context losses, never utilization, power, temperature or memory.
For those, also run the reporter's `axon-inference --server <label>` collector on the
inference host: AMD sysfs (`gpu_busy_percent`, VRAM and GTT counters, hwmon
temperature and power) and `nvidia-smi` where present. Dedicated VRAM and GTT
allocation are shown separately because an APU keeps its weights in GTT; neither is a
spending limit. One host may report both ways; whichever read arrived last wins.

### Through Prometheus

If a Prometheus already scrapes those servers, point Axon at it instead:

```sh
axon-server --inference neuralforge=http://neuralforge:13308/metrics \
  --prometheus http://localhost:9090
```

Prometheus must label each server's series `instance="<label>"`, the same label
given to `--inference`. llama.cpp-style servers reset their tokens-per-second gauges
whenever `/metrics` is read, so two readers garble each other's speed. With
`--prometheus`, Axon stops reading the endpoint and computes speed from the running
counters (`rate()` over a minute). It reads the endpoint directly only when Prometheus
is unreachable or holds no fresh scrape of that label.

Prometheus also adds history. The panel keeps GPU use and a few headline numbers
(generation speed, running/queued requests, GPU memory) on the left, with one chart of
output tokens per second on the right over the top bar's time horizon (Any time shows
30 days). Hover the chart to read any step. A footer gives the prompt and output tokens
the server handled in that span. Both come from
`GET /resources/history?account=<label>&range=<seconds>`, which returns up to 120 steps
of output rate, requests, KV cache and GPU busy, power and temperature, plus the span's
token totals. Each step holds the peak for load measures and the average for hardware.
GPU power and temperature come from node-exporter's `amdgpu` hwmon sensors on the same
`instance`. GPU busy appears once node-exporter runs with `--collector.drm`. Without
`--prometheus` the endpoint returns 404 and panels show live readings only.

A panel turns Stale thirty seconds after the last reading. An endpoint that answers
with anything other than usable metrics is Offline, not a row of zeros. Load stays
aggregate: it cannot say which of Hermes, Pi or OpenCode caused it. See the reporter
[inference collectors](../packages/python/axon_reporter/README.md#shared-inference-servers).

## Work status and session history

The board groups sessions by host, then agent, and defaults to the last hour.
The selected time horizon applies to every session, including sessions with older
requests or queued messages. There is no review queue or acknowledgment workflow.

Explicit work updates after a completion supersede its earlier outcome. Session
details label that outcome as superseded; generic Stop/heartbeat hooks still
preserve valid completions. This also corrects existing retained history.

Timeline and session history default to meaningful reports. Include lifecycle
details reveals hook events, with filtering applied before pagination. Message
status distinguishes queued from accepted by the agent; acceptance does not
prove an answer or completed action.
