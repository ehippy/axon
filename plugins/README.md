# Agent plugins

Codex, Claude Code, Hermes, Pi, and OpenCode use native adapters with one
shared reporter runtime. Built packages contain protocol/reporter code only;
no server modules, query client, or neighboring source checkout are required
at runtime.

```sh
python3 plugins/tools/manage.py build --output /tmp/axon-plugins
```

This creates `axon-codex`, `axon-claude`, `axon-hermes`, `axon-pi`, and
`axon-opencode` directories. Rebuild instead of editing artifacts. Rebuilding
a recognized package replaces it cleanly so old runtime files cannot remain.
Arbitrary nonempty directories are rejected.

Native layouts are root `plugin.json` plus hooks/skill for Codex,
`.claude-plugin/plugin.json` plus hooks/skill for Claude, `plugin.yaml` plus
CLI/gateway hooks for Hermes, a Pi package manifest (`package.json` with
`pi.extensions`) plus an in-process `index.ts` extension for Pi, and a plain
JS module auto-loaded from opencode's global plugins directory for OpenCode.
Use your agent's native plugin installation flow, or the managed installer:

```sh
python3 plugins/tools/manage.py install codex          # preview only
python3 plugins/tools/manage.py install codex --apply
python3 plugins/tools/manage.py install claude --apply
python3 plugins/tools/manage.py install hermes --apply
python3 plugins/tools/manage.py install pi --apply
python3 plugins/tools/manage.py install opencode --apply
```

The installer manages packages under `~/.local/share/axon/plugins/<agent>`,
preserves unrelated user settings/hooks, and backs up changed settings/packages
under `~/.local/state/axon/install-backups/`. Repeated installation is idempotent.
`--home PATH` stages a different home; for Hermes also use `--no-enable` unless
you intend to enable the plugin in the active Hermes profile. Use
`--hermes-command PATH` to select the executable. Installation does not restart
agents or edit Codex trust state. Review/trust changed hooks and reload agents.
Choose native loading or the managed installer for each agent, not both.

## Keeping machines current

`sync` is the canonical way to keep every machine's agents reporting after
the checkout moves or a new agent appears. Keep a dedicated clone (never a
working checkout) and let a user timer run it, the same pattern as the
server's `axon-update.timer`:

```sh
SRC="$HOME/.local/share/axon/src"
git clone -q https://github.com/ehippy/axon "$SRC"
python3 "$SRC/plugins/tools/manage.py" install-timer --apply   # --interval MINUTES, default 15
```

On Linux this writes `axon-sync.service`/`.timer` to `~/.config/systemd/user`
and enables them; on macOS it writes and bootstraps the launchd agent
`~/Library/LaunchAgents/org.axon.sync.plist` (log in `~/.local/state/axon/sync.log`).
Both bake in the installing shell's PATH so agents are detected; rerun
`install-timer --apply` after moving them. Without `--apply` it prints the plan.
Templates are in [deploy](deploy).

Each run is `manage.py update --apply`: refuse if the clone has local changes,
`git pull --ff-only`, then never go past the journal server's deployed commit
(`GET <AXON_SERVER or http://AXON_BROKER:8510>/update`) — when the pulled HEAD
is not in that commit's history, the clone is reset to the server's commit, so
hooks that add protocol fields wait for the server deploy. An unreachable server
is logged and the current hooks are kept; a server commit missing from the clone
undoes the pull and fails the run. Then `sync --apply` runs from the updated code.
The result lands in `~/.local/state/axon/sync-status.json`
(`revision`, `at`, `ok`, `error`); `manage.py status` prints it.

Bootstrap steps such as chezmoi `run_after` install the timer instead of syncing
hooks themselves. That keeps every unattended sync behind the server check:

```sh
git -C "$SRC" pull -q --ff-only 2>/dev/null || git clone -q https://github.com/ehippy/axon "$SRC"
python3 "$SRC/plugins/tools/manage.py" install-timer --apply
```

Sync detects installed agents (PATH plus `~/.local/bin`,
`~/.npm-global/bin`, `~/.opencode/bin`), installs for each, and stamps
`~/.local/state/axon/hooks-synced` (honors `AXON_ACTIVITY_HOME`) with the
checkout revision plus the detected set — later runs skip when it matches,
so the cost of frequency is one `git pull`. A failing agent does not block
the others; the stamp is only written when all succeed, so the next run
retries. Exit code 2 means something failed; `--force` reinstalls, and
without `--apply` sync only reports the plan. Restart agents to load hooks;
Codex will ask to trust changed ones.

## Taking a host out

`uninstall` is the inverse of `install` and `install-timer`, because removal has the
same five vendor shapes and the same risk of deleting something that is not ours. It
uses the installer's own predicate: a hook is ours only if its command runs
`~/.local/share/axon/plugins/…/hooks/run.py`, and a path is ours only if it is a symlink
into that payload. Every other hook, environment key and config line survives.

```sh
python3 "$SRC/plugins/tools/manage.py" uninstall              # plan for every agent
python3 "$SRC/plugins/tools/manage.py" uninstall all --apply  # unregister, stop timers
python3 "$SRC/plugins/tools/manage.py" uninstall all --apply \
  --purge --archive "$HOME/axon-archive"                      # also delete payload + journal
```

Without `--apply` nothing is written and the plan lists each file it would touch and
what it would remove. `--apply` backs up every rewritten file to
`~/.local/state/axon/config-backups/pre-uninstall-<stamp>` first. It then, in order:

1. deletes `~/.config/axon/auto-report`, so a concurrent chezmoi apply cannot reinstall;
2. unregisters each agent — Claude hooks plus `env.AXON_*` and the usage directory in
   `CLAUDE_CODE_PLUGIN_DIRS`; Codex hooks, the `[shell_environment_policy.set]` transport
   block and, when the hooks file held nothing else, the file itself plus the Codex trust
   hashes that named it; Hermes `AXON_*` lines (every other line stays) and the plugin
   symlink in each profile, asking `hermes plugins disable` first when the binary is found;
   Pi's extension and skill symlinks; OpenCode's plugin and skill symlinks;
3. removes the `install-timer` unit, booting it out first (`org.axon.sync` on macOS,
   `axon-sync.timer` on Linux) so the next tick cannot reinstall what was just cleaned;
4. removes companion jobs it did not install but can prove are Axon's — a launchd agent
   whose `ProgramArguments` name `~/.local/share/axon` or `axon-runner`, the matching
   plists, and the `axon-runner` uv tool. A Linux unit that `install-timer` did not write
   is listed under `manual_units` and left running, because on a server host it is
   probably the journal itself.

`--purge` deletes `~/.local/share/axon`, `~/.local/state/axon` and `~/.config/axon`, and
refuses to guess what that means for the data: pass `--archive DIR` (copies the host
credential, `*.sqlite3`, `sync-status.json` and the pre-uninstall backups into a `0700`
directory named for the host label, files `0600`) or `--discard-data`. It also refuses
when `~/.local/share/axon/server` exists, because that host is the journal other hosts
report to; stop the server deliberately, then pass `--i-am-the-server`. `uninstall pi`
limits the first step to one agent. Exit code 2 means at least one agent was left
installed (a malformed config is reported, not repaired). The Codex path parses TOML, so
it needs Python 3.11+; other agents install on 3.10+. Reload running agent sessions
afterwards: a session opened before the cleanup still carries the reporting instructions
in its context and will keep trying to report.

Configuration: Python 3.10+, `mosquitto_pub`, `AXON_BROKER`, `AXON_PORT`,
`AXON_ACTIVITY_HOME`, optional `AXON_MOSQUITTO_PUB`, `AXON_MACHINE`, `AXON_SOURCE`.
Install and sync add `AXON_MACHINE=auto` to `~/.config/axon/env` (or
`AXON_CONFIG_FILE`) when no label exists, so the board shows host names instead of
the private install ID; `auto` re-resolves per report, a literal label pins one.
Nothing is written when a label already exists, on a dry run, or when the agent
install itself fails. Labels only affect later events, so adopting one leaves
earlier rows under the previous host value until retention clears them. `sync`
ensures the label on every applied run, including stamp-skipped ones, so existing
machines pick it up without `--force`.
See [reporter documentation](../packages/python/axon_reporter/README.md).
Hooks receive the environment of the host agent process; reload it after changing
configuration. Agent commands name their exact session; no global latest-session
variable is used. Lifecycle handlers ignore raw prompts, responses, and tool args.
Explicit finished outcomes require evidence; Stop means waiting.

This release removes the gateway-only adapter and source-tree compatibility
scripts. See [migration](../docs/migration.md) before replacing an old installation.
