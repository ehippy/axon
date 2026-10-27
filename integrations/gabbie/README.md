# Gabbie adapter

Gabbie is a separate application responsible for voice recognition, inference,
speech playback, and agent-message routing. Axon does not install or import it.

On a voice-enabled handset, configure:

```ini
AXON_VOICE_ENABLED=true
AXON_DEVICE_ID=handset-a
AXON_VOICE_ENDPOINT=127.0.0.1:8765
```

Gabbie's local event server should emit one JSON object per line with an `event`
field. Accepted display events are thinking/speaking/confirming/done_speaking.
Ambient listening/transcribing events are ignored. Button-down listening feedback
is local, so it remains immediate.

Configure Gabbie's MQTT adapter to consume **`axon/handsets/handset-a/ptt`**,
with nonretained `start`/`stop` payloads. This replaces the former global topic;
update the external Gabbie application before enabling voice after migration.
The topic is scoped by device but the external voice adapter still owns how many
concurrent devices it supports. Axon does not implement multi-device audio mixing.

The optional [relay helper](relay/SKILL.md) documents delivery of queued messages
into explicitly selected agent sessions. Set `AXON_SSH_TARGET` locally and adjust
its queue path to your deployment. Nothing installs or activates this skill
implicitly. Do not commit relay queues or deployment addresses.
