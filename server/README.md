# Axon server

The `axon-server` Python package consumes activity events, stores SQLite history,
expires leases, applies retention, and publishes session views. It depends only
on `axon-protocol` plus Python's standard library. Mosquitto command-line clients
provide MQTT transport; display and agent-plugin code are not dependencies.

See [installation](../docs/install-server.md), [architecture](../docs/architecture.md),
and [protocol](../protocol/README.md).
