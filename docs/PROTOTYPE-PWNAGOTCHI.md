# Portable display prototype

This prototype repurposes a Raspberry Pi Zero 2 W, display HAT, and PiSugar3
battery controller as a portable agent status display. A Raspberry Pi 4 build
is described separately in [SPEC.md](SPEC.md).

## Hardware and drivers

- Raspberry Pi Zero 2 W with Raspberry Pi OS Lite, 64-bit.
- Current display: PiSugar Whisplay HAT (240×280 color LCD, RGB LED, button,
  microphone, and speaker). Use the upstream Whisplay driver and verify SPI,
  audio, and GPIO access for the service account.
- Earlier display: Waveshare 2.13-inch e-Paper V3. Select `epd2in13_V3`;
  the V2 driver can run without errors while producing no visible refresh.
- PiSugar3 provides battery telemetry and power/button management. Confirm
  controller identity and firmware before selecting an I2C address or updater.
- Display rotation and panel offsets depend on the enclosure and driver.

## Services and configuration

`software/axon-native/` implements the native display daemon.
`software/display-service/` retains the Python implementation and preview tools.
The display exposes a loopback HTTP API on port 8500 for health, status, and
button events. Keep this API local unless you provide appropriate access controls.

The display and reporting adapters use MQTT. Set `AXON_BROKER` in each process's
local environment (or a local systemd override) to select a broker; the default
is `localhost`. The journal also accepts `--broker`. Broker credentials, host
addresses, remote-access configuration, and SSH destinations belong in local
configuration rather than this repository. Configure broker authentication and
network access controls for your deployment.

For the activity journal service, create a dedicated `axon` account, install the
checkout under `/opt/axon`, or adjust the example unit to your installation.
See [the journal guide](../software/activity-journal/README.md) and
[plugin installation guide](../software/plugins/README.md).

## Agent status contract

Legacy publishers send retained JSON to `axon/agents/<source>/status`:

```json
{"state": "running", "task": "Updating display", "ts": 1700000000}
```

Use deployment-selected source labels. Publisher timestamps let displays expire
stale state after reconnecting. The journal publishes richer per-session rows to
`axon/activity/view`; these replace the corresponding aggregate rows.
Codex, Claude Code, and Hermes have adapters in `software/`.

A stationary broker can coordinate portable displays without depending on the
display's uptime. Enable broker persistence if retained state and durable
subscriptions must survive broker restarts. Verify reconnect behavior by
restarting the broker and confirming that publishers and displays recover.

## Gabbie and Glass integrations

Gabbie can run alongside the display and use its microphone, speaker, and button.
The native display consumes Gabbie's loopback event stream on port 8765.
The push-to-talk bridge publishes ordered `start`/`stop` events to `axon/ptt`
through one persistent MQTT connection, avoiding ordering races between separate
TCP connections. Voice-state timeouts return the display to idle if events stop.
Glass can publish status through the same agent-status contract.

## Implementation notes

- Keep one process responsible for the panel to avoid competing refreshes.
- The earlier e-Paper implementation needed partial refreshes with periodic full
  refreshes; the color LCD renders complete frames.
- Use one shared status palette for screen and LED feedback.
- Button scripts may strip JSON quotes; the loopback GET event endpoint supports
  simple gesture commands when a JSON POST is unsuitable.
- Keep firmware, audio, display, and battery checks independent when diagnosing
  hardware faults. Verify the driver against the actual hardware revision.
- Store custom artwork and generated previews outside tracked source files.
- Keep personal activity databases, relay queues, logs, and deployment settings
  outside Git. Do not grant passwordless administrative access merely to run a
  display or reporting service.
