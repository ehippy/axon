# Axon for Codex

Native codex lifecycle reporting and session-scoped semantic guidance.
Build this directory with [the plugin manager](../README.md), which bundles its
protocol and reporter dependencies. Source development loads those dependencies
from `packages/python/`; built artifacts run without the source checkout.

No server process or server code is bundled. Observer failures do not stop agent
work. Reload the host agent after installation; review/trust changed hooks when
required by the host.

## Dashboard wake (opt-in, via Codex's own queue)

Set `AXON_WAKE=codex` (or add `codex` to its comma-separated list) in the
reporter's host settings. When a turn ends cleanly, the Stop hook detaches one
idle watcher per thread; it long-polls the authenticated inbox on the
`codex-queue` wake path and hands whatever arrives to
`codex queue --thread <id> --message …`. Whichever client holds the thread
runs it as a turn in front of the user. The watcher only works while a client
has the thread loaded: Codex holds an flock on
`~/.codex/thread-writer-locks/<id>.lock` while it does. Once nothing holds the
lock, the watcher stops and hands back any message it claimed, so the host
runner can resume the thread (see [runner/README.md](../../runner/README.md#resuming-ended-sessions-18)).
It also stops when you type into the thread (local input wins), or after
`AXON_LISTEN_SECONDS` (default 3 hours).
Also enable **Allow dashboard messages to wake this session** in the
session's dashboard detail. Both switches default off; the real thread UUID
never leaves this host — the server only sees the hashed session key. A
failed `codex queue` call releases the lease, so the message is retried
instead of dropped. The dashboard message answers in-turn while the thread
is busy, exactly as before.
