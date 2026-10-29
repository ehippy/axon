# axon-runner

The host job runner for issue #19: it long-polls the Axon journal for start
jobs addressed to this machine and does only what a host-local config allows.
The host pulls; nothing on the host listens for inbound connections.

Ships **disabled**: with no `~/.config/axon/runner.toml` (or an empty allowlist)
the runner claims jobs and refuses every one with `not_allowed`, so nothing is
ever dropped silently.

## Config — `~/.config/axon/runner.toml`

```toml
server = "http://broker:8510"     # optional; falls back to AXON_SERVER, then http://$AXON_BROKER:8510

[[allow]]
project = "axon"                  # bare project name as the dashboard shows it
path = "~/Projects/axon"          # host-local; paths never leave this machine
agents = ["claude"]               # empty list = any agent with an executor
permission_mode = "dontAsk"       # Claude only; default shown
sandbox = "workspace-write"       # Codex only; read-only or workspace-write

worktree_root = "~/.local/state/axon/worktrees"  # optional; where runner-managed
                                                 # git worktrees live (Pi jobs);
                                                 # default shown
```

`[executable]` maps an agent name to a specific binary (tests and pilots);
unset agents use the CLI on `PATH`.

## Credentials

Reuses the #14 host credential at `~/.config/axon/host-credential`
(override with `AXON_HOST_CREDENTIAL_FILE`). The machine name defaults to the
hostname or `AXON_MACHINE`; claims only ever return jobs addressed to that
machine with this credential.

## Job lifecycle

`queued → claimed → started | refused`, and `started → finished | failed` once
an executor reports back. Refusal reasons are reported verbatim:
`not_allowed`, `project_path_missing`, `no_executor`, `launch_failed: <tail>`,
`worktree_setup_failed: <tail>`, `launch_timeout`.

Implemented executors: **claude** (`claude --bg -n <name> -w <name>
--permission-mode <mode> "<prompt>"` in the project directory, checked against
CLI 2.1.312; Claude creates its own worktree), **codex** (`codex exec
--json -s <sandbox> -C <path> [--worktree] "<prompt>"`, checked against CLI
0.162.1; the job reports started once the JSONL stream yields `thread.started`,
and the session's own hooks put it on the board) and **pi** (`pi -p
--session-id axon-<job> "<prompt>"`, checked against CLI 1.4.0; the session id
is derived from the job so the Pi plugin reports it under a stable key, and
because Pi has no native worktree flag a worktree job runs inside a
runner-managed `git worktree` under `worktree_root`). Bypass flags are never
passed, and a `sandbox` config value outside read-only/workspace-write is
refused with `bad_sandbox`. OpenCode and Hermes land as follow-up increments
and are refused with `no_executor` until then.

The Codex child keeps running after the runner reports started; its `--json`
output is appended to a host-local log under
`$AXON_ACTIVITY_HOME/runner/job-<id>.log` (default `~/.local/state/axon/`)
instead of a pipe, so an orphaned child can never block on a closed reader.

## Running

```sh
axon-runner            # foreground loop (long-poll 55s)
axon-runner --once     # claim at most one job and exit
```

User service: copy `deploy/axon-runner.service` (after `pipx install ./runner`
or equivalent) and `systemctl --user enable --now axon-runner.service` —
enabling it is the explicit opt-in; nothing installs it automatically.
