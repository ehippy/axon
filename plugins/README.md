# Agent plugins

Codex, Claude Code, and Hermes use native adapters with one shared reporter
runtime. Built packages contain protocol/reporter code only; no server modules,
query client, or neighboring source checkout are required at runtime.

```sh
python3 plugins/tools/manage.py build --output /tmp/axon-plugins
```

This creates `axon-codex`, `axon-claude`, and `axon-hermes` directories. Rebuild
instead of editing artifacts. Rebuilding a recognized package replaces it cleanly
so old runtime files cannot remain. Arbitrary nonempty directories are rejected.

Native layouts are root `plugin.json` plus hooks/skill for Codex,
`.claude-plugin/plugin.json` plus hooks/skill for Claude, and `plugin.yaml` plus
CLI/gateway hooks for Hermes. Use your agent's native plugin installation flow,
or the managed installer:

```sh
python3 plugins/tools/manage.py install codex          # preview only
python3 plugins/tools/manage.py install codex --apply
python3 plugins/tools/manage.py install claude --apply
python3 plugins/tools/manage.py install hermes --apply
```

The installer manages packages under `~/.local/share/axon/plugins/<agent>`,
preserves unrelated user settings/hooks, and backs up changed settings/packages
under `~/.local/state/axon/install-backups/`. Repeated installation is idempotent.
`--home PATH` stages a different home; for Hermes also use `--no-enable` unless
you intend to enable the plugin in the active Hermes profile. Use
`--hermes-command PATH` to select the executable. Installation does not restart
agents or edit Codex trust state. Review/trust changed hooks and reload agents.
Choose native loading or the managed installer for each agent, not both.

Configuration: Python 3.10+, `mosquitto_pub`, `AXON_BROKER`, `AXON_PORT`,
`AXON_ACTIVITY_HOME`, optional `AXON_MOSQUITTO_PUB`, `AXON_MACHINE`, `AXON_SOURCE`.
See [reporter documentation](../packages/python/axon_reporter/README.md).
Hooks receive the environment of the host agent process; reload it after changing
configuration. Agent commands name their exact session; no global latest-session
variable is used. Lifecycle handlers ignore raw prompts, responses, and tool args.
Explicit finished outcomes require evidence; Stop means waiting.

This release removes the gateway-only adapter and source-tree compatibility
scripts. See [migration](../docs/migration.md) before replacing an old installation.
