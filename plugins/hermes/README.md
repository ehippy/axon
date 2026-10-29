# Axon for Hermes

Native hermes lifecycle reporting and session-scoped semantic guidance.
Build this directory with [the plugin manager](../README.md), which bundles its
protocol and reporter dependencies. Source development loads those dependencies
from `packages/python/`; built artifacts run without the source checkout.

No server process or server code is bundled. Observer failures do not stop agent
work. Reload the host agent after installation; review/trust changed hooks when
required by the host.

## Native dashboard wake (opt-in)

Add `hermes` to `AXON_WAKE` in the host reporter settings, then reload Hermes.
Enable **Allow dashboard messages to wake this session** in the dashboard using
the input token. Both switches default off. Sync must have enrolled the host's
private claim credential.

Between turns, the plugin long-polls the authenticated inbox and uses
`ctx.inject_message` with the captured session route. During a turn it pauses
native injection and returns claimed input to the inbox for the existing
pre-model, tool-result, and final-verify delivery hooks. This matters because native injection into a busy classic CLI
interrupts work; the plugin never uses that path while a tool can be running.

TUI, desktop, gateway-hosted CLI, and messaging sessions additionally require native Hermes consent:

```yaml
plugins:
  entries:
    axon-activity:
      allow_gateway_injection: true
```

Missing consent or an unavailable session route leaves messages queued and
reports the wake availability reason in session activity. Reload, finalization,
and classic-CLI session reset stop the bridge; old sessions cannot inject into a
replacement CLI session. Paths and native route IDs stay on the host.
