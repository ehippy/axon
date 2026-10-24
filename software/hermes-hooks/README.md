# Axon for Hermes

`plugin.yaml` and `__init__.py` provide a native Hermes plugin for both CLI and
gateway sessions. `pre_llm_call` supplies session-specific reporting guidance;
observer hooks track tools, waiting, failures, interruptions, teardown, and child
sessions. Callback failures are isolated, and raw messages, responses, arguments,
child goals and child summaries are never copied into activity records.

Use [the plugin manager](../plugins/README.md) to build or install/enable it. The
installer backs up the old gateway-only `axon-status/HOOK.yaml` and removes it
from discovery after the native plugin is enabled. Restart the gateway to load
the new plugin. The `axon-status/` source remains for rollback; do not activate
both implementations simultaneously.
