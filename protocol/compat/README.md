# Previous-release compatibility

`previous/` is an unmodified snapshot of `axon_protocol` from commit
`936afb2093193fd7c5a2d69b2d9f7a8a6e486769` (the #22 compatibility release).
It is test data, never installed or imported by the reporter or server.

CI executes the frozen validator against current events and validates previous
release fixtures with the current validator. Keep the snapshot independent of
the working implementation; update its provenance when advancing the baseline
to a later deployed protocol release.
