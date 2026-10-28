# Axon for Claude

Native claude lifecycle reporting and session-scoped semantic guidance.
Build this directory with [the plugin manager](../README.md), which bundles its
protocol and reporter dependencies. Source development loads those dependencies
from `packages/python/`; built artifacts run without the source checkout.

No server process or server code is bundled. Observer failures do not stop agent
work. Reload the host agent after installation; review/trust changed hooks when
required by the host.
