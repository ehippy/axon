# Install the handset

The supported hardware binary targets Linux with a PiSugar Whisplay display,
GPIO/SPI access, PiSugar battery service, and optional Gabbie. It needs no server
Python package or database. Install/configure a broker separately.

Build on the target Raspberry Pi (Rust toolchain required):

```sh
cargo build --locked --release --features hardware --manifest-path handset/native/Cargo.toml
```

The binary is `handset/native/target/release/axon-native`. The bundled font is
embedded. Panel dimensions, pin mapping, and SPI speed are defined in
[profile.rs](../handset/native/src/profile.rs); check these against your board.
The initial supported profile is Whisplay, not a generic hardware autodetector.
Enable SPI and install/configure the PiSugar battery service separately.

Deploy the binary to `/opt/axon/bin/axon-native` and copy
[axon-handset.service](../handset/deploy/axon-handset.service) into your systemd
configuration. The example runs as root for hardware/shutdown access; a dedicated
account requires separately granting device access and shutdown policy.
No broad passwordless sudo rule is required by this repository.

Put local configuration in `/etc/axon/handset.env`:

```ini
AXON_DEVICE_ID=handset-a
AXON_BROKER=localhost
AXON_PORT=1883
AXON_HTTP_LISTEN=127.0.0.1:8500
AXON_VOICE_ENABLED=false
AXON_VOICE_ENDPOINT=127.0.0.1:8765
```

Set a distinct device ID per handset; it labels the display and scopes PTT.
MQTT connection IDs also include a process-instance suffix to avoid collisions.
Voice is **off** by default: no voice TCP connection or PTT publisher/button
handler is started. See [Gabbie](../integrations/gabbie/README.md) to enable it.
Broker/API addresses and IDs are validated before hardware initialization.

The local control API accepts `/health`, POST `/event` with a button gesture,
and GET `/event/<gesture>`. A long gesture shuts the device down. Keep the API
on loopback. The server API is independent on port 8510.

## Check without hardware

```sh
cargo test --locked --manifest-path handset/native/Cargo.toml
cargo run --locked --example project --manifest-path handset/native/Cargo.toml < protocol/fixtures/view.json
```

These compile/test the portable library, rendering, state, protocol mapping, and
voice decisions without GPIO or a server. The actual binary requires the
`hardware` feature and Linux. Before deploying, physically check rotation,
battery, RGB LEDs, local gestures, voice press/release, and reconnect behavior.
