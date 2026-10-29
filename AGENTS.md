# Working on Axon

Run `python3 tools/test.py` and `python3 tools/check_privacy.py` before pushing.

CI has two paths: `Components` runs on every change except those touching only
`handset/**`, and `Handset` runs on `handset/**` and `protocol/**` (or
`gh workflow run handset.yml`). Rust builds therefore never delay a server deploy, but
a commit mixing server and handset changes waits for both.

The server host follows `main` on its own (see [docs/install-server.md](docs/install-server.md#automatic-updates)):
a server change goes live once CI passes. After pushing one, ask for the deploy rather
than waiting for the five-minute timer, from the host named by `AXON_BROKER` in your
Axon reporter command, so the input token never leaves it:

```sh
ssh "$HOST" 'curl -sX POST -H "Authorization: Bearer $(cat ~/.config/axon/input-token)" http://127.0.0.1:8510/update'
ssh "$HOST" 'curl -s http://127.0.0.1:8510/update'
```

The second command shows whether your commit is deployed (`commit`), still waiting
(`requested_at`) or refused because CI failed or the health check did (`failed`).
Do not deploy by hand.
