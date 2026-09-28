---
name: gabbie-relay
description: Check gabbie's relay queue on axon for messages she couldn't deliver herself, and deliver them into the right Claude Code session. Use when asked to "check the relay queue" / "check on gabbie's relay" / "deliver gabbie's messages", or as the per-cycle task of the standing gabbie-relay loop.
---

# gabbie-relay

Gabbie (the voice assistant running on axon) can't call `send_message`
herself - only an interactive Claude Code session can, and she isn't one.
When the user tells her "tell workstation to X," her `relay_to_agent` tool
just appends a line to a queue file on axon. This skill is the other half:
drain that queue and actually deliver each entry via `send_message`.

Never guess who a message is for. Delivering to the wrong session is a
real mistake - a message with the user's real instructions landing in
someone else's work. If the target doesn't resolve cleanly, leave it
queued and say so; don't send it anywhere.

## Canonical targets

Matching is by **exact session title**, case-insensitive - not fuzzy
substring matching. Session titles are normally auto-generated per
conversation topic, so this only works because sessions meant to be
relay targets get manually retitled to a fixed name via
`set_session_title` (e.g. this project's main session on workstation is
titled exactly "workstation"). If a `target` doesn't exactly match any
open session's title, treat it as unresolved - don't fall back to
guessing from a partial/topic-based title.

## Steps

1. **Read the queue.**
   ```
   ssh pi@device.example "cat /home/pi/gabbie/relay_queue.jsonl 2>/dev/null"
   ```
   Each line is one JSON object: `{"target", "message", "ts", "delivered"}`.
   If the file doesn't exist or every line has `"delivered": true`, there's
   nothing to do - report that plainly (or say nothing at all, if this is
   a quiet loop cycle - see the loop's own instructions for how to report
   a no-op tick) and stop here.

2. **For each line where `"delivered": false`:**
   - Call `list_sessions` to see what's currently open (list is small;
     don't cache across queue entries within the same run - a session may
     have opened or closed between entries).
   - Match `target` against session titles **exactly**, case-insensitive
     only (see Canonical targets above) - not a substring/fuzzy match.
   - **Exactly one exact match** - call `send_message(session_id, message)`
     verbatim (don't editorialize, summarize, or add framing - it's the
     user's own words, relayed).
   - **Zero matches** - leave it queued (don't mark delivered). Either the
     target session isn't open right now, or it hasn't been retitled to
     its canonical name yet.
   - **More than one exact match** (shouldn't normally happen if titles
     are kept unique) - leave it queued. Don't guess between them; surface
     the ambiguity to the user instead.

3. **Rewrite the queue file** reflecting what actually got delivered this
   pass (successfully-delivered entries get `"delivered": true`; anything
   left unresolved stays `false` untouched, exactly as it was, so the next
   pass retries it). Write the whole updated file back:
   ```
   ssh pi@device.example "cat > /home/pi/gabbie/relay_queue.jsonl" <<'EOF'
   <updated JSONL content>
   EOF
   ```

4. **Report** what happened this pass: what got delivered and to where,
   and what's still stuck waiting (with why - no matching session found,
   or ambiguous match) so the user can act on anything genuinely stuck.
