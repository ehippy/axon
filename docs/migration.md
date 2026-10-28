# Migrating to the component breakout (0.3)

This is an intentional break. There are no old-path wrappers, Python display
fallback, gateway-only Hermes handler, or reporter aggregate-status publisher.
Do not point existing services at removed `software/` paths.

1. Back up the journal with SQLite's backup API and save installed artifacts,
   service units, and agent settings. The database schema is unchanged, but
   startup retention still applies. Test an independent copy first.
2. Build new artifacts without installing into live agent settings. Stop the old
   journal and install the independent server package/unit. Never run two
   collectors against the same broker/subscription identity.
3. Replace the handset binary/unit. Build with `--features hardware` on Linux.
   Use a deployment-selected `AXON_DEVICE_ID`. Voice is now off by default.
4. Change external Glass/Gabbie status publishers to
   `axon/integrations/<source>/status` with a sender timestamp. Configure Gabbie
   for `axon/handsets/<device-id>/ptt` before enabling handset voice. The external
   integration applications require their own configuration/code changes.
5. Rebuild/reinstall native plugins, then reload the agent hosts. Remove any
   manually installed old source-tree hooks or gateway-only Hermes hook; the
   installer only replaces its recognized managed-package hook commands.
   Do not leave native loading and managed hooks enabled together.
6. Choose `AXON_MACHINE` / `AXON_SOURCE` labels or let each installation generate
   a private random identity. Existing host-derived records remain history and
   age through the normal leases/retention; no private database rewrite occurs.
   Label changes can temporarily show old and new rows until old leases expire.
7. Verify reporter events reach the server, session views reach the handset, and
   integration status/voice controls use the new topics. Check physical display,
   battery, LEDs, and shutdown controls before relying on the deployment.

For rollback, stop the new services, restore saved artifacts/units/settings and
external topic configuration, and use the backed-up database if needed. Do not
reset an active outbox just to roll back; preserve queued event IDs. No live
service, agent profile, private database, or external integration is modified by
this repository refactor itself.
