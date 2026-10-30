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

## Resuming ended sessions (#18)

A message sent from the dashboard (your phone included) to a session that has
ended on this host resumes it, if all of these hold:

1. the session has **wake** allowed (session details, needs the input token);
2. nothing in the session can take the message itself: no armed listener or
   Codex queue watcher, and not mid-turn (the journal waits 10s for one first);
3. `runner.toml` has a `[resume]` section listing the agent.

```toml
[resume]
agents = ["claude", "codex"]      # default when the section is present
permission_mode = "dontAsk"       # Claude: default; bypassPermissions is refused
sandbox = "workspace-write"       # Codex: read-only or workspace-write
```

No section means no resuming. The journal hands over Axon's hashed session key
only. The runner finds the agent's own id and folder in the agent's local store
(`~/.claude/projects`, `~/.codex/sessions`), so ids and paths stay on the host.

- **Claude:** if `claude agents --json` still lists the session it is open, so
  `--resume` would fork a copy, and the runner refuses with `session_open`.
  Otherwise it runs `claude --bg --resume <id> --permission-mode <mode>
  "<message>"` in the session's folder. The resumed turn reports through its own
  hooks, and `claude attach <id>` opens it on the host.
- **Codex:** if a client holds the thread's writer lock
  (`~/.codex/thread-writer-locks/<id>.lock`), the message goes to `codex queue`
  and becomes that client's next turn. Otherwise the runner runs
  `codex exec resume --json -c sandbox_mode="<sandbox>" <id> "<message>"`
  headless, which also drains anything already in the thread's Codex queue. The
  output goes to `$AXON_ACTIVITY_HOME/runner/resume-<key8>.log`.

`dontAsk` lets an unattended turn use only the tools your Claude permission
rules already allow, and denies the rest without prompting. Nothing waits on an
approval nobody can give from the phone. A refusal (`session_open`,
`session_not_found`, `project_path_missing`, `liveness_unknown`,
`launch_failed: <tail>`, ...) shows on the session's message panel. The message
stays queued and is retried after the next message or 10 minutes.

## Credentials

Reuses the #14 host credential at `~/.config/axon/host-credential`
(override with `AXON_HOST_CREDENTIAL_FILE`). The machine name is the one the reporter
uses: `AXON_MACHINE` from the environment or `~/.config/axon/env`, else the short
hostname (also for `auto`). A macOS hostname can change on its own, so pin the
label there. Claims only ever return jobs addressed to that machine with this
credential.

## Starting sessions from the dashboard

Each claim also sends the host's start offers: the `[[allow]]` project names
with the agents that have an executor, leaving out any entry whose folder is
missing. Paths are never sent. While the runner keeps polling, the board shows
that host with a **New session** button, even when nothing runs there. Pick a
project and agent, write the prompt, and choose whether it gets its own
worktree (the default). Starting needs the input token. The form shows the
job's state: queued, starting, started, or the refusal reason. The new session
appears on the board once its own hooks report. Project pages list the same
agents under each host. A runner that stops polling for 3 minutes stops being
offered.

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

macOS: fill in `deploy/org.axon.runner.plist` (`{runner}` is the installed
`axon-runner`, `{path}` a PATH that finds `claude` and `codex`, `{log}` a log
file), copy it to `~/Library/LaunchAgents/`, and
`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/org.axon.runner.plist`.
`uv tool install ./runner` installs `axon-runner` to `~/.local/bin`.

Linux: fill in `deploy/axon-runner.service` the same way (`{runner}` and
`{path}`; output goes to the journal, `journalctl --user -u axon-runner`), copy it
to `~/.config/systemd/user/`, and `systemctl --user enable --now axon-runner.service`.
On a headless host, `loginctl enable-linger $USER` keeps it running without a login.
`KillMode=process` lets resumed agents outlive a runner restart. Enabling either
service is the explicit opt-in; nothing installs it automatically.
