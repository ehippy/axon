# Development and releases

## Component checks

The default path covers everything but the handset. It runs on every push and pull
request, and skips commits that only touch `handset/**`:

```sh
python3 tools/test.py
python3 tools/check_privacy.py
```

Python tests use temporary state, never installed agent profiles. Install the
Mosquitto broker and command-line clients for the real outage/restart test; that
test is explicitly skipped if unavailable.
No server/handset is deployed by these checks.

## Handset checks

The handset is a separate path (`.github/workflows/handset.yml`) that runs on pushes
and pull requests touching `handset/**` or `protocol/**` — the wire contract the device
consumes — and whenever it is dispatched by hand:

```sh
cargo test --locked --manifest-path handset/native/Cargo.toml
cargo build --locked --example project --manifest-path handset/native/Cargo.toml
AXON_HANDSET_PROJECTOR="$PWD/handset/native/target/debug/examples/project" python3 tools/test.py
gh workflow run handset.yml          # or: from the Actions tab
```

`AXON_HANDSET_PROJECTOR` enables the actual Rust consumer in the broker test, so that
workflow sets it and installs Mosquitto: the broker-to-handset path stays covered
there instead of on every server commit. The hardware binary is a separate
Linux build with `--features hardware`. Verify it on the target physical board
before release; automated builds cannot validate wiring or device behavior.

## Build and check release artifacts

```sh
python3 -m venv .venv-build
.venv-build/bin/pip install setuptools wheel
.venv-build/bin/python tools/build.py --output dist/0.3.0
.venv-build/bin/python tools/verify_release.py dist/0.3.0
python3 tools/check_privacy.py --release dist/0.3.0
```

Build output must be empty. The builder stages copies in temporary directories
so setuptools does not pollute source trees. It creates four independent wheels,
three self-contained plugin archives, and a manifest containing protocol version
and SHA-256 checksums. It does not publish packages or install live hooks.
The verifier installs server/reporter/query in separate temporary virtualenvs with
no network/package index and checks that unrelated component modules are absent.

For a full release, build a Linux aarch64 handset executable and pass its path
with `--handset-binary`. `gh workflow run handset.yml` performs that build with a
cross-toolchain and uploads `handset-linux-aarch64`, so a release that skips that run
ships without the hardware binary. The
manifest indicates whether the hardware binary is included. Use artifacts from
the same release; protocol v1 remains the event/session-view contract. The
integration-status and device-scoped voice topics use the 0.3 contract, so old
publishers require migration. Python runtime is 3.10+; build/check tooling uses
Python 3.11+.

The GitHub Actions workflow runs these checks and uploads artifacts; it does not
publish a release or deploy services. Tag/publish only after hardware validation
and the [migration checklist](migration.md). Privacy pattern checks cover private
addresses and local hostnames, not arbitrary secrets or semantic personal data;
also run a redacting secret scanner and review changes before publishing.
