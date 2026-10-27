# Glass status adapter

Glass can publish a retained status message to `axon/integrations/mirror/status`.
Use the broker connection selected by your deployment:

```json
{"state":"running","task":"Rendering mirror","ts":1700000000}
```

Replace `ts` with the current Unix timestamp in seconds on every update. Valid
states are idle/running/error. The handset rejects missing timestamps and expires
silent integration rows. Use a stable source label in the topic; `mirror` selects
the Glass display color. This feed is display status, not durable work history.
Publish activity events through the reporter if journaled sessions are needed.
The former `axon/agents/.../status` feed is not consumed in this release.
