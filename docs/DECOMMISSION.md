# Axon decommission runbook

Audited 2026-11-15 against the live hosts. Every path below was read or listed, not
assumed. Order matters: **kill the resurrection paths before cleaning anything**, or
the installers put the hooks back within an hour.

Nothing here is executed by this document. Each phase is a separate, reviewable step.

**2026-11-17 update:** `manage.py uninstall` now automates most of Phase 3 and the agent
half of Phase 4 for an agent host, and the Mac carries a fourth installer this document
did not list (`org.axon.sync`). On an agent host the short path is:

```sh
python3 ~/.local/share/axon/src/plugins/tools/manage.py uninstall            # plan
python3 ~/.local/share/axon/src/plugins/tools/manage.py uninstall all --apply \
  --purge --archive "$HOME/axon-archive"
```

Phase 0 (decisions), Phase 2 (preserve, unless `--archive` is enough), the server host
`crumb`, and Phase 5 (GitHub, LAN exposure) remain manual. Phases 1 and 3-4 below stay
as the manual fallback and as the audit of what the command claims to do.

## Footprint (verified)

### Hosts

| Host | Role | Axon services |
| --- | --- | --- |
| `crumb` | server + broker | `axon-server`, `axon-activity`, `axon-relay-hermes`, `axon-runner` (user, enabled); `axon-sync.timer`, `axon-update.timer` (user, enabled); `mosquitto.service` (system, enabled) |
| `PromptForge` (this Mac) | agent host | launchd `org.axon.runner`, `org.axon.resources.codex`, `org.axon.sync` (15 min installer, added later); live `axon-runner` process (uv tool); OpenCode plugin and skill symlinks present |
| `NeuralForge` | agent host + inference | `axon-inference.service`, `axon-sync.timer` (user, enabled) |

Listeners: `0.0.0.0:8510` (axon-server) and `0.0.0.0:1883` (mosquitto) on `crumb`, both
publicly bound on the LAN. `crumb` is also on Tailscale, so both are reachable on its
100.64/10 CGNAT range as well; addresses are left out of this file deliberately, since
`tools/check_privacy.py` rejects private ranges in source.

### Dotfiles (`~/.local/share/chezmoi`)

| File | Change |
| --- | --- |
| `run_after_50-axon-agent-hooks.sh` | **delete the whole script** — the only chezmoi-managed Axon target |
| `justfile:23` | drop "axon hooks" from the `scripts` recipe comment |
| `Brewfile:23-24` | drop the `mosquitto` line and its Axon comment |
| `Pacfile:26-27` | same |
| `README.md:57-58` | drop `50` from the after-script ordering list |
| `README.md:152-177` | delete the "Axon agent hooks" section |
| `run_onchange_after_90-install-packages.sh.tmpl:10-11` | reword the comment; keep the ordering rationale, drop the axon/hermes numbering example that no longer applies |

### Agent config entries (PromptForge; same shapes on other hosts)

| File | Axon content |
| --- | --- |
| `~/.claude/settings.json` | 8 hook entries across `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PermissionRequest`, `Stop` ×2, `SessionEnd`; plus `env.AXON_BROKER`, `env.AXON_PORT` |
| `~/.codex/hooks.json` | 8 Axon hook entries |
| `~/.codex/config.toml:132-136` | the `# Axon reporting transport…` comment + entire `[shell_environment_policy.set]` block (contains only the four `AXON_*` keys — safe to remove whole) |
| `~/.hermes/.env:569-570` | `AXON_BROKER`, `AXON_PORT` only — do **not** touch the other 567 lines |
| `~/.pi/agent/extensions/axon-activity` | symlink → `~/.local/share/axon/plugins/pi` (this is what injects reporting instructions into every Pi session) |
| `~/.pi/agent/skills/axon-activity` | symlink → `…/plugins/pi/skills/axon-activity` |
| `~/.config/opencode` | plugin `axon-activity.js` and `skills/axon-activity` symlinks were installed here on 2026-11-03, contrary to the original note; confirm per host |
| `~/.claude/skills/axon-activity`, `~/.codex/skills/axon-activity` | skill symlinks into the payload, on top of the hook entries above |

### Local state and payloads

- `~/.local/share/axon` — 39 MB (`plugins/`, `src/` managed clone, `host-source/`, `resources/`, `private-backups/`)
- `~/.local/state/axon` — 46 MB (`outbox.sqlite3`, `activity.sqlite3`, `host-registry.sqlite3`, `identity.sqlite3`, `listeners/`, `runner/`, `config-backups/`, `source-backups/`, `sync.log`, `hooks-installed`, `hooks-synced`)
- `~/.config/axon` — `env`, `runner.toml`, **`host-credential`** (this host's claim token)
- `~/Library/LaunchAgents/org.axon.runner.plist`, `org.axon.resources.codex.plist`
- `axon-runner` installed as a uv tool (`~/.local/bin/axon-runner`, `~/.local/share/uv/tools/axon-runner`)
- `crumb`: `~/Projects/axon`, `~/.local/share/axon/{server,updater,runner,plugin-staging}`, `~/.local/state/axon/*.sqlite3`

### Verified as Axon-only (safe to remove)

`mosquitto` — its only consumers are `journal.py`'s `mosquitto_pub` publish and the
reporters. No Hermes, Glass, Gabbie, or shell config publishes to 1883 independently.
`~/.local/bin/bd` matched a grep only as a binary byte offset; not a dependency.

---

## Phase 0 — Decisions (blockers, do these first)

1. **Repo: archive or delete?** Recommend archive on GitHub (read-only, keeps 151
   commits and the hardware spec) plus a local `git bundle`. Deleting throws away
   `docs/SPEC.md` and `docs/PROTOTYPE-PWNAGOTCHI.md`, which are the reusable parts.
2. **Journal: keep the history?** 552 sessions/7 d, 30 d retained. Recommend a
   compressed dump — it's the only evidence base for a post-mortem, and the corpus is
   interesting regardless.
3. **Handset and hardware**: box it, don't reuse `crumb`'s slot until Phase 5 is done.
4. **`crumb`'s 8510/1883 LAN exposure**: this is the one item that argues for doing
   Phase 1 even if everything else is postponed.

## Phase 1 — Stop resurrection paths (do not clean before this)

Three independent installers will rewrite agent configs after you clean them:

```sh
# 1. crumb — the 5-minute self-updater that follows main
ssh crumb 'systemctl --user disable --now axon-update.timer axon-sync.timer'

# 2. NeuralForge — the 15-minute plugin resync
ssh neuralforge 'systemctl --user disable --now axon-sync.timer axon-inference.service'

# 3. PromptForge (Mac) — the runner that can *start* agent sessions, and the
#    quota poller
launchctl bootout gui/$(id -u)/org.axon.runner 2>/dev/null
launchctl bootout gui/$(id -u)/org.axon.resources.codex 2>/dev/null
pkill -f 'axon-runner' 2>/dev/null
```

Then, in the same sitting, delete `run_after_50-axon-agent-hooks.sh` from chezmoi and
commit. Until that script is gone, `com.dotfiles.chezmoi-fresh` (hourly, `RunAtLoad`)
runs `chezmoi apply`, which calls `manage.py sync --apply`, which reinstalls every
hook. This is the single most likely way a decommission silently undoes itself.

Any other agent host: `systemctl --user disable --now axon-sync.timer` before Phase 3.

## Phase 2 — Preserve (before any deletion)

```sh
mkdir -p ~/axon-archive && cd ~/axon-archive

# journal + projections, while the server is still up
for ep in "sessions?period=0" "events?period=0&limit=100000" "projects?since=0" resources; do
  curl -s -m 30 "http://crumb:8510/$ep" -o "$(echo $ep | tr '?/=' '___').json"
done

# the database itself, authoritative
ssh crumb 'cd ~/.local/state/axon && sqlite3 activity.sqlite3 ".backup /tmp/activity.sqlite3"'
ssh crumb 'gzip -c /tmp/activity.sqlite3' > crumb-activity.sqlite3.gz

# source bundle
cd ~/Projects/axon && git tag decommissioned && git bundle create ~/axon-archive/axon.bundle --all
```

Then write a short `POSTMORTEM.md` while the reasoning is fresh — see Phase 6 for the
three findings worth keeping.

## Phase 3 — Clean agent configs (per host)

`manage.py uninstall all --apply` does everything in this phase and reports a plan first;
what follows is the manual equivalent and the predicate it must match.

Back up first, then remove **only** Axon-owned entries. The installer's own marker is
the path `/.local/share/axon/plugins/` — reuse that predicate rather than editing by
hand, so unrelated hooks survive.

```sh
H=~/.local/state/axon/config-backups/pre-decommission-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$H" && cp ~/.claude/settings.json ~/.codex/hooks.json ~/.codex/config.toml ~/.hermes/.env "$H"/
```

Then, per agent:

- **Claude** — drop hook entries whose `command` contains `/.local/share/axon/plugins/`;
  delete `env.AXON_BROKER` and `env.AXON_PORT`. Leave all other hooks and keys.
- **Codex** — same hook filter in `~/.codex/hooks.json`; delete the
  `[shell_environment_policy.set]` block and its comment; also drop the matching
  `[hooks.state."…/hooks.json:stop:0:0"]` `trusted_hash` entries that point at Axon,
  so a stale trust hash isn't left behind.
- **Hermes** — delete only the two `AXON_*` lines.
- **Pi** — `rm ~/.pi/agent/extensions/axon-activity ~/.pi/agent/skills/axon-activity`
  (symlinks only). This is what removes the ~320 tokens of standing reporting
  instructions from every future Pi session.
- **OpenCode** — remove the plugin under `~/.config/opencode` if that host has one.

Also stop Axon's background listeners before deleting their code, so nothing is left
detached with a dead interpreter:

```sh
pkill -f 'axon_reporter/native_inbox' 2>/dev/null; pkill -f 'codex-queue' 2>/dev/null
ls ~/.local/state/axon/listeners 2>/dev/null
```

**Reload running agents afterwards.** A session that started before Phase 3 still
carries the reporting instructions in its context and will keep trying to report.

## Phase 4 — Remove software

PromptForge (Mac):

```sh
rm ~/Library/LaunchAgents/org.axon.runner.plist ~/Library/LaunchAgents/org.axon.resources.codex.plist \
   ~/Library/LaunchAgents/org.axon.sync.plist
uv tool uninstall axon-runner
rm -rf ~/.local/share/axon ~/.local/state/axon
brew uninstall mosquitto
```

`~/.config/axon` last, on every host — `host-credential` and `env` are the only things
left that identify this machine, and once the repo is archived you cannot re-enroll
without them. Copy them into `~/axon-archive/` (0600) or shred:

```sh
cp -pr ~/.config/axon ~/axon-archive/config-axon && chmod -R go-rwx ~/axon-archive
rm -rf ~/.config/axon
```

crumb:

```sh
ssh crumb 'systemctl --user disable --now axon-server axon-activity axon-relay-hermes axon-runner'
ssh crumb 'rm -f ~/.config/systemd/user/axon-*.{service,timer} && systemctl --user daemon-reload'
ssh crumb 'rm -rf ~/.local/share/axon ~/.local/state/axon ~/.config/axon'
ssh crumb 'sudo systemctl disable --now mosquitto && sudo apt-get remove -y mosquitto mosquitto-clients'
# leave ~/Projects/axon until Phase 0 decision and the bundle are verified
```

NeuralForge:

```sh
ssh neuralforge 'rm -f ~/.config/systemd/user/axon-*.{service,timer} ~/.config/systemd/user/axon-inference.service && systemctl --user daemon-reload'
ssh neuralforge 'rm -rf ~/.local/share/axon ~/.local/state/axon ~/.config/axon'
```

`crumb` also served inference/quota panels: confirm `axon-inference` and any Prometheus
target you added for Axon are removed there, not on the inference hosts themselves.

## Phase 5 — Close the outside edge

- GitHub: archive (or delete) per Phase 0; close #12, #18, #19, #23 with a comment
  saying the project is decommissioned and pointing at `docs/DECOMMISSION.md`.
- Delete any router port-forward, uptime check, or DNS record aimed at 8510/1883.
- The input token dies with the server, but check `~/.config/axon` copies and any
  password manager entry for a stored Axon input token.
- If a phone has "Axon" added to the Home Screen, remove it — it will otherwise point
  at a dead `crumb`.

## Phase 6 — Verify (each must pass)

```sh
# no axon left in agent config or dotfiles
grep -ril axon ~/.claude/settings.json ~/.codex ~/.hermes/.env ~/.pi/agent/extensions \
  ~/.pi/agent/skills ~/.config/opencode ~/.local/share/chezmoi 2>/dev/null
chezmoi apply --dry-run            # no axon script, no mosquitto
```

- Open a fresh Pi, Claude, and Codex session on PromptForge and NeuralForge: no Axon
  reporting paragraph in context, no hook fires, no `report.py` invocation.
- `ssh crumb 'ss -ltnp | grep -E "8510|1883"'` → empty.
- Reboot `crumb` and the Mac: nothing Axon starts.
- One chezmoi cycle completes clean (`chezmoi-fresh` exit 0, no axon in its log).

## What the first host actually took (PromptForge, 2026-11-17)

`manage.py uninstall all --apply --purge --archive ~/axon-archive` ran clean, `failed: {}`.
Four things worth knowing before doing another host:

- Four registrations were not hook entries and needed their own rules, all now covered:
  Claude's `statusLine` (it ran the payload's `axon-resources`, so it would have failed on
  every render once the payload was gone), a hand-written `~/.codex/hooks/codex_axon.py`
  predating the installer, one Codex sandbox rule that allowed the reporter's exact
  command line, and the `# Axon reporting transport.` header left above the Hermes `.env`
  lines once the lines under it went.
- `--archive` copies what identifies the host and its journals — `~/.config/axon`,
  `*.sqlite3`, `sync-status.json`, `config-backups/` — which was 664 KB. It did not keep
  the rest of the 46 MB `~/.local/state/axon`: `runner.log`, `sync.log`, `runner/`,
  `listeners/`, `resources/`, `codex-desktop/`, `install-backups/`, `source-backups/`.
  Decide per host whether any of that matters before `--purge`.
- Nothing was lost unpublished: the outbox held 0 rows. Check the same way on another host
  before purging — `sqlite3 ~/.local/state/axon/outbox.sqlite3 'select count(*) from outbox'`.
- Left in place deliberately: `~/Projects/axon` (now ahead of the deployed server commit,
  so hosts held at that commit cannot run `uninstall` until the server deploys), and
  `mosquitto` until `brew uninstall` says so.
- A session opened before the cleanup still carries the reporting instructions and can
  rewrite `~/.local/state/axon` as it is deleted. The report now names that under
  `reappeared`; restart the agent, then remove what came back.

## Phase 7 — Findings to keep (write these down)
1. **It was a write-only system by arithmetic, not by neglect.** 552 sessions / 7 d,
   6 host labels, 151 project labels, 63 % of them Hermes automation. No human reads
   that, so nobody looked — the correct response to the design.
2. **Tokens were spent on the wrong side of the pipe.** ~48 % of sampled events were
   LLM-authored narration and ~320 tokens of standing instructions went into every
   session in every project, to describe things a hook already knew for free. Nothing
   read the output.
3. **The control plane was the cost, not the value.** Wake, inbox, resume, and runner
   drove every hard problem (inbox auth, host claims and leases, token handling) and
   five vendor-specific glue paths that would each have broken on the next upstream
   change — and "being removed from the development" was the felt problem all along.

If Axon is ever revisited, the salvageable ideas are: mechanical hook-side telemetry
with zero LLM involvement, a *reader* agent that verifies claimed outcomes against
`git log`/tests rather than summarising them, and the filter **cheap to do remotely and
expensive to defer** for deciding what a phone should ever be allowed to touch.
