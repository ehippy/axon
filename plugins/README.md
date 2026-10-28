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
working checkout), then run sync from your bootstrap loop — chezmoi
`run_after`, cron, login hook — as often as you like:

```sh
SRC="$HOME/.local/share/axon/src"
git -C "$SRC" pull -q --ff-only 2>/dev/null || git clone -q https://github.com/ehippy/axon "$SRC"
python3 "$SRC/plugins/tools/manage.py" sync --apply
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
