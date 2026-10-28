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
the loopback API should use a secured tunnel; the API has no authentication.

## Browser UI

Open `http://127.0.0.1:8510/` on the server, or forward that loopback port over
SSH and open the forwarded local address. The packaged, read-only UI offers Now,
a filterable event timeline, and session details with evidence. The board groups
live reporting sessions by host, then agent, with a separate tile for each
source/session pair. Tile order is stable across activity updates. Agent identity
colors follow the handset palette; status dots separately indicate work state.
Host labels come from reported `machine` values (`AXON_MACHINE` can set a friendly
label); missing host metadata is shown explicitly instead of inferred. It polls every
five seconds; paging into older timeline events pauses refresh until you click
Refresh. Heartbeats are hidden by default. Session links encode source and
session in the URL fragment, so dashboards can link directly to a session.

`--no-web` disables browser assets while preserving the JSON API. No CDN, frontend
build step, or extra service is needed. The UI uses the server's existing listen
address and authentication model: keep it on loopback or behind authenticated
access, since activity can contain personal project details. Agent-reported text
is displayed as text; evidence becomes a link only for explicit HTTP(S) URLs.

The board keeps sessions in their host/agent groups for one hour after their last
report, including finished and disconnected sessions. Live state still follows
the reported lease; keeping a tile visible does not extend that lease. Older
sessions are available in the collapsed older-session section and timeline.

Within each host and agent group, live sessions appear first; sessions in each
section are ordered by most recent report, with session identity breaking ties.

Hosts occupy side-by-side board columns when space permits. Agent groups and
session tiles stack vertically inside each host; narrow screens stack hosts.
