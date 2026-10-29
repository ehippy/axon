# Install agent plugins

Build/load just the plugins on agent hosts; no journal server or handset code is
needed. Follow [the plugin guide](../plugins/README.md) for native packages or the
managed installer. The reporter can also be installed as a standalone CLI; see
[its guide](../packages/python/axon_reporter/README.md). To keep agent hosts
current automatically with a user timer installed from bootstrap, follow
[Keeping machines current](../plugins/README.md#keeping-machines-current).

## Host claim credentials

Sync enrolls a host once the server advertises host enrollment in `/health`.
It reads the input token on the broker over SSH and stores only the resulting
host claim credential in `~/.config/axon/host-credential` (mode 600). Later syncs
reuse it. `python3 plugins/tools/manage.py enroll-host` can enroll explicitly.
For installations without broker SSH, set `AXON_INPUT_TOKEN_FILE` to a local
input-token file during enrollment. `AXON_HOST_CREDENTIAL_FILE` overrides the
claim credential location. Never put either credential in an activity event.

The first authentication release accepted and logged legacy claims. After that
migration release, host credentials are required by default. A temporary
`--allow-legacy-claims` server flag permits only old in-turn claims during a
remaining migration; unattended wake always requires a host credential and
session consent. Invalid supplied credentials always fail.
Claims belong to the machine in the session's last report. Changing a machine
label requires enrolling under the new label.

The input-token holder can revoke a credential with
`POST /hosts/credentials/revoke` and a JSON `credential_id` (the SHA-256 of the
credential). Delete the revoked local file and enroll again to rotate it.
`POST /sessions/<source>/<key>/wake` with `{"wake": true}` requires the input token;
wake defaults to false. Enrollment alone never enables unattended wake.

Unattended delivery uses `/inbox/wake/claim` and `/inbox/wake/ack`. A claim leases
messages for 90 seconds. They remain pending until the native API accepts them;
failed delivery or an expired lease lets them be retried. Acknowledgments are
idempotent and atomically record an audit event naming the dashboard input token
and native delivery path, without copying the message into activity history.
