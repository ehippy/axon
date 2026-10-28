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

The session detail view can send messages into running Claude Code sessions.
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

Codex and Hermes sessions cannot receive input yet.

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

## Shared inference servers

Every shared inference host runs one `axon-inference --server <label>` collector and
displays its own panel: running and queued requests, generation and prompt speed,
KV-cache use, then per-GPU utilization, power, temperature and memory. The label is
a stable identity (`neuralforge`, `moar`), so another server means another panel
rather than another integration.

Hardware counters are read on the host: AMD sysfs (`gpu_busy_percent`, VRAM and GTT
counters, hwmon temperature and power) and `nvidia-smi` where present. Dedicated
VRAM and GTT allocation are shown separately because an APU keeps its weights in
GTT; neither is a spending limit. Request and throughput figures come from the
GUFO/llama.cpp metrics endpoint, `http://127.0.0.1:13308/metrics` by default.

A snapshot publishes about every ten seconds and its panel turns Stale after thirty
seconds without one; an unreachable endpoint reports Offline while hardware readings
carry on. Load stays aggregate: it cannot say which of Hermes, Pi or OpenCode caused
it. See the reporter [inference collectors](../packages/python/axon_reporter/README.md#shared-inference-servers).
