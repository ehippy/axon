# Axon agent plugins

Codex, Claude Code, and Hermes now share one reporting runtime with separate
native plugin packages. Each session gets its own reporting command. Hooks
publish private lifecycle facts; model-visible guidance asks the agent to report
its goal, meaningful milestones, blockers, and an evidence-backed outcome.
There is no transcript scraping, automatic success inference, or extra model call.

## Build portable packages

```sh
python3 software/plugins/manage.py build --output /tmp/axon-plugins
```

This creates `axon-codex`, `axon-claude`, and `axon-hermes`, each with a bundled
`runtime/`. Copy a package anywhere: it does not import files from neighboring
repos. Source checkouts use the shared `software/activity-journal` directly.
Generated packages should not be edited; rebuild them after runtime changes.

The native formats are:

- Codex: root `plugin.json`, `hooks/hooks.json`, and a reporting skill.
- Claude: `.claude-plugin/plugin.json`, `hooks/hooks.json`, and a reporting skill.
- Hermes: `plugin.yaml`, `__init__.py`, and CLI/gateway observer hooks.

For Claude's native loader, use `claude --plugin-dir /tmp/axon-plugins/axon-claude`.
For Codex, distribute the built directory through its plugin installation flow.
Native packages and the compatibility installer are alternatives: do not enable
both for the same agent on the same machine.

## Install on a machine

```sh
# Preview paths without writing anything:
python3 software/plugins/manage.py install codex
# Install hooks + skill, preserving other hooks/settings:
python3 software/plugins/manage.py install codex --apply
python3 software/plugins/manage.py install claude --apply
# Install/enable the native Hermes plugin with its own CLI:
python3 software/plugins/manage.py install hermes --apply
```

Codex/Claude installation copies a self-contained package to
`~/.local/share/axon/plugins/<agent>`, merges hooks into their user settings, and
installs the reporting skill by symlink. Only recognized previous Axon hook
commands are replaced. Other hooks, permissions, notifications and preferences
are preserved. Repeated installation produces the same hook configuration.

Hermes installation links the package into `~/.hermes/plugins/axon-activity` and
runs `hermes plugins enable axon-activity`, preserving other enabled plugins.
Use `--hermes-command /path/to/hermes` when its executable isn't on PATH, or
`--no-enable` to stage without enabling. After enabling, the old gateway-only
`axon-status/HOOK.yaml` is moved into the backup so both integrations do not report
the same sessions. The older handler source stays available for rollback.
Hermes's native hooks cover CLI and gateway, not just messaging sessions.

Restart/resume Codex and Claude sessions after installing. Codex requires the user
to review/trust the changed hook definitions; the installer does not edit trust
state. Restart Hermes's gateway after installation to load its new plugin.
The installer itself does not restart running agents.

`--home PATH` targets a different home for configuration and dry-run tests.
Hermes's CLI enable step uses the active Hermes profile; when staging another
home use `--no-enable`, or run the installer as that user with its profile active.
Existing settings, previous packages and replaced skills are backed up under
`~/.local/state/axon/install-backups/<timestamp>-<agent>/` before changes. To roll
back, restore that backup's settings and package, then reload the agent. For
Hermes also restore `legacy-HOOK.yaml` to its original hook directory and restore
its saved `config.yaml` if returning to the gateway-only integration.

Prerequisites: Python 3.10+, `mosquitto_pub`, access to an MQTT broker on port 1883.
Set `AXON_BROKER` in the publisher and display process environments to select
a remote broker; the default is `localhost`. The journal also accepts `--broker`.
No Python packages are needed by the plugins or installer. Reporter environment
settings (`AXON_BROKER`, `AXON_PORT`, `AXON_ACTIVITY_HOME`, `AXON_MOSQUITTO_PUB`)
work in all three agents. Session commands include the exact session ID explicitly;
no global "latest session" variable is used, so concurrent chats stay distinct.

## Report semantics

At SessionStart and UserPromptSubmit, Codex/Claude receive bounded context with a
shell-quoted command using that chat's session ID. Hermes gets the same context
from `pre_llm_call`. No user prompt or response is copied into that context or
published by the lifecycle integration. The model explicitly writes the short
semantic fields through its terminal tool.

Tool heartbeats are limited to one every 30 seconds per source/session. Lifecycle
transitions bypass throttling. Follow-up turns preserve the recorded goal until
the agent explicitly starts a new task. Missing cwd does not erase a known project.
Git branch metadata is collected at user-turn boundaries when cwd is available.
Hermes child-session records link to hashed parent identities without copying
child goals or summaries from event payloads.

`Stop`/successful turn finalization means waiting, not finished. Failed Hermes
turns are blocked; interruptions are interrupted; teardown is stopped. Explicit
finished outcomes require a result and evidence before they enter the local
outbox. Later lifecycle hooks preserve a completed outcome until a new start.

The reporter also retains an aggregated `axon/agents/<source>/status` with the
original `{state,task,ts}` shape so existing displays still work. Multiple chats
are aggregated without one waiting chat marking another working chat idle. Rich
session events and durable history continue through the activity journal.

## Test

```sh
python3 -m unittest discover -s software/plugins/tests -v
python3 -m unittest discover -s software/activity-journal/tests -v
```

Tests cover portable bundles, context isolation/quoting, privacy, heartbeat
throttling, follow-up field preservation, concurrent legacy aggregation, truthful
completion, fail-open behavior, idempotent installation with unrelated hooks,
invalid-config protection, and Hermes lifecycle and child identity mapping.
The shared journal tests include real broker delivery and restart recovery.

## Format references

- [Codex plugin packaging](https://developers.openai.com/plugins/build/plugins)
- [Codex hook context and trust](https://learn.chatgpt.com/docs/hooks)
- [Claude plugin manifests](https://code.claude.com/docs/en/plugins-reference)
- [Claude hook context](https://code.claude.com/docs/en/hooks)
- [Hermes plugins](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins)
- [Hermes lifecycle contracts](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks)
