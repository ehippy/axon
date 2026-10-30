# Axon for Claude

Native claude lifecycle reporting and session-scoped semantic guidance.
Build this directory with [the plugin manager](../README.md), which bundles its
protocol and reporter dependencies. Source development loads those dependencies
from `packages/python/`; built artifacts run without the source checkout.

No server process or server code is bundled. Observer failures do not stop agent
work. Reload the host agent after installation; review/trust changed hooks when
required by the host.

`usage/` is a separate hooks-module plugin loaded through `CLAUDE_CODE_PLUGIN_DIRS`,
so the lifecycle hooks above are never registered twice. It publishes Claude
subscription allowance snapshots (see [account resources](../../packages/python/axon_reporter/README.md#account-resources)).

## Sharing the conversation

Set `AXON_TRANSCRIPT=claude` in the reporter's settings (`~/.config/axon/env`) to
show the real back-and-forth in the dashboard's session chat. Each turn sends only
your prompts and Claude's text replies; tool calls, tool output, thinking and
injected context are skipped and never sent. Before anything leaves the host, known secret
shapes (cloud and API keys, bearer tokens, private keys, URL passwords,
`password=`-style values, long random strings) and this host's own Axon
credentials are replaced with `[redacted]`. That masking is a best effort, so the
setting is off by default and the server keeps the text for `--transcript-days`
(default 7). Text goes over HTTP with the host credential, never over MQTT.
