# Component breakout implementation

The original plan was revised to allow breaking APIs and remove superseded
implementations rather than carrying a compatibility layer.

## Delivered boundaries

1. Protocol fixtures and validation shared by producer/server/handset tests.
2. Dependency-free protocol package and independently installable reporter.
3. Standalone server and query client, with installed entry points/service units.
4. Portable handset display/state core plus a Linux-only hardware binary.
5. Native plugins bundling only protocol/reporter code; optional integrations
   documented separately. The old Python display, gateway hook, aggregate feed,
   old-path scripts, and no-op status endpoint are removed.
6. Independent wheel/plugin builds, CI checks, and explicit migration/rollback
   guidance. Release artifacts carry protocol compatibility and checksums.

## Deliberate changes

- Reporter identity defaults to a stable private installation ID, not a hostname.
- Voice is opt-in and PTT topics are scoped to device IDs.
- Integration status has its own topic namespace and requires sender timestamps.
- Directory paths and entry points change together; no live deployment is altered.

See [architecture](architecture.md), [development](development.md), and
[migration](migration.md) for the implementation and deployment checks.
Physical handset validation and changes to external Gabbie/Glass deployments
remain deployment work and are not claimed by automated tests.
