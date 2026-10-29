"""Shared, fail-open integration runtime for Axon agent plugins."""
from contextlib import closing
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlencode
import uuid

from . import report
from .credentials import read_credential

# Agents whose hooks can take dashboard input as in-turn context. Claude also has an idle
# listener (asyncRewake). Codex cannot wake an idle thread, so its Stop hook takes input that
# arrived late in the turn. Hermes injects in-process via pre_llm_call, transform_tool_result
# and pre_verify; input queued while idle arrives when the session next runs.
INBOX_AGENTS = {'claude', 'codex', 'hermes'}
LISTEN_AGENTS = {'claude'}
# Codex cannot rewake from a hook, but a durable per-thread queue wakes the
# thread that has it open (or the next client that loads it). The watcher is a
# detached child of the Stop hook and hands input to `codex queue`.
QUEUE_AGENTS = {'codex': 'codex-queue'}
# Stays below the bundled Stop listener's hook timeout.
LISTEN_CAP = 14000


def context(agent, session):
    if not session:
        return ''
    settings = [f'{name}={os.environ[name]}' for name in ('AXON_BROKER', 'AXON_PORT', 'AXON_MACHINE', 'AXON_SOURCE', 'AXON_ACTIVITY_HOME') if name in os.environ]
    command = shlex.join((['/usr/bin/env', *settings] if settings else []) + [sys.executable, str(Path(report.__file__).resolve()), '--agent', agent, '--session', str(session)])
    return (
        'Axon activity reporting is enabled for this session. Report a short semantic goal before substantive work, '
        'then significant milestones, direction changes, or blockers. Use your existing terminal tool with this command prefix: '
        + command + '. Append: start --project PROJECT --goal "Desired outcome" --step "Current step"; '
        'progress --step "Meaningful progress"; waiting --step "Decision needed"; blocked --blocker "Dependency"; '
        'or finished --outcome "Concrete result" --evidence "Actual commit, test, PR, or artifact reference". '
        'Report finished only when the requested work is complete, before your final response. '
        'For a conversation without substantive work, lifecycle status is enough. '
        'Use progress to refine the goal during the same task, and start --goal when beginning or resuming work. '
        'Hooks already track lifecycle and generic tools; do not report each tool call. '
        'Do not include credentials, raw prompts, transcripts, command arguments, or unverified success claims. '
        'A reporting failure must not block the task; reports queue locally for retry. '
        'Do not invent a session ID or use another chat\'s reporting command.'
    )


def branch(cwd):
    if not cwd:
        return ''
    try:
        p = subprocess.run(['git', '-C', cwd, 'branch', '--show-current'], capture_output=True, text=True, timeout=.25)
        return p.stdout.strip()[:200] if p.returncode == 0 else ''
    except (OSError, subprocess.TimeoutExpired):
        return ''


def heartbeat_due(agent, session, now=None):
    """Limit tool chatter to one heartbeat per 30s per chat; lifecycle never throttled."""
    now = time.time() if now is None else now
    source, key, _ = report.identity(agent, str(session))
    with closing(sqlite3.connect(report.spool_path(), timeout=.2)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS hook_heartbeats(source TEXT, session TEXT, at REAL, PRIMARY KEY(source,session))')
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT at FROM hook_heartbeats WHERE source=? AND session=?', (source,key)).fetchone()
        if row and 0 <= now - row[0] < 30:
            return False
        db.execute('INSERT OR REPLACE INTO hook_heartbeats VALUES(?,?,?)', (source,key,now))
        return True


def lifecycle(agent, data):
    """Only identifiers, cwd, event and tool names are read, plus usage counts from the transcript; other payloads are ignored."""
    session = data.get('session_id')
    event = data.get('hook_event_name')
    if not session:
        return
    safe = {key: data[key] for key in ('session_id','cwd','hook_event_name','tool_name','transcript_path') if key in data}
    if event in ('PreToolUse','PostToolUse') and not heartbeat_due(agent,session):
        return
    if event == 'UserPromptSubmit':
        # A new user turn may continue the same task. Do not erase its semantic goal.
        patch = {'state':'working', 'activity':'working', 'user_turn':True}
        if agent in INBOX_AGENTS:
            patch['listening_until'] = 0
        if safe.get('cwd'):
            current_branch = branch(safe['cwd'])
            if current_branch:
                patch['branch'] = current_branch
        parent = data.get('parent_session_id')
        if parent:
            source, hashed, _ = report.identity(agent,str(parent))
            patch['parent'] = f'{source}/{hashed}'
        report.report(agent,str(session),'progress',patch,cwd=safe.get('cwd'))
    else:
        report.hook(safe,agent)


def server_url():
    return report.setting('AXON_SERVER') or 'http://%s:8510' % report.setting('AXON_BROKER', 'localhost')


def wake_enabled(agent):
    return agent in re.split(r'[,\s]+',report.setting('AXON_WAKE','').strip())


def claim(agent, session, wait=0, timeout=None, wake_path=None):
    """Take this session's pending dashboard messages from the journal server."""
    source, key, _ = report.identity(agent, str(session))
    credential = read_credential()
    action='inbox/wake/claim' if wake_path else 'inbox/claim'
    query=urlencode({'wait':f'{wait:g}',**({'path':wake_path} if wake_path else {})})
    request = urllib.request.Request(f'{server_url().rstrip("/")}/sessions/{source}/{key}/{action}?{query}',
                                     data=b'', method='POST',
                                     headers={'Authorization': 'Bearer '+credential} if credential else {})
    with urllib.request.urlopen(request, timeout=timeout or wait + 5) as response:
        return json.load(response).get('messages', [])


def acknowledge_wake(agent, session, messages, accepted=True):
    credential=read_credential()
    if not credential:
        raise ValueError('Host credential required')
    source,key,_=report.identity(agent,str(session))
    payload={'ids':[m['id'] for m in messages],'claim_token':messages[0]['claim_token'],'accepted':accepted}
    request=urllib.request.Request(f'{server_url().rstrip("/")}/sessions/{source}/{key}/inbox/wake/ack',
        data=json.dumps(payload).encode(),headers={'Authorization':'Bearer '+credential,'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=5) as response:
        return json.load(response)


def delivery(messages):
    lines = [time.strftime('[%H:%M] ', time.localtime(m['at'])) + m['text'] for m in messages]
    return ('The user sent this to this session from the Axon dashboard. '
            'It is the user\'s own input; respond to it as you would a typed message:\n' + '\n'.join(lines))


def inbox_due(agent, session, now=None):
    """In-turn inbox checks run at most every 10s per chat so tool calls stay fast."""
    now = time.time() if now is None else now
    source, key, _ = report.identity(agent, str(session))
    with closing(sqlite3.connect(report.spool_path(), timeout=.2)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS hook_inbox(source TEXT, session TEXT, at REAL, PRIMARY KEY(source,session))')
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT at FROM hook_inbox WHERE source=? AND session=?', (source,key)).fetchone()
        if row and 0 <= now - row[0] < 10:
            return False
        db.execute('INSERT OR REPLACE INTO hook_inbox VALUES(?,?,?)', (source,key,now))
        return True


def listener_path(agent, session):
    source, key, _ = report.identity(agent, str(session))
    return report.spool_path().with_name('listeners') / f'{source}.{key}'


def arm_listener(agent, session):
    """Register this listener as the session's only one; earlier listeners notice and exit."""
    path = listener_path(agent, session)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    token = uuid.uuid4().hex
    tmp = path.with_name(f'{path.name}.{token}.tmp')
    tmp.write_text(token)
    tmp.replace(path)
    return token


def cancel_listener(agent, session):
    try:
        listener_path(agent, session).unlink()
    except OSError:
        pass


def armed(agent, session, token):
    try:
        return listener_path(agent, session).read_text() == token
    except OSError:
        return False


def listen(agent):
    """Background Stop hook: wait for dashboard input, then wake the idle session with exit code 2."""
    try:
        data = json.load(sys.stdin)
        session = data.get('session_id') if isinstance(data, dict) else None
        seconds = min(LISTEN_CAP, max(0, int(report.setting('AXON_LISTEN_SECONDS', '10800'))))
        if session and agent in LISTEN_AGENTS and not wake_enabled(agent):
            cancel_listener(agent,session)
        if not session or agent not in LISTEN_AGENTS or not seconds or not wake_enabled(agent):
            return 0
        poll = min(50, max(1, int(report.setting('AXON_LISTEN_POLL', '50'))))
        token = arm_listener(agent, session)
        deadline, renewed = time.time() + seconds, 0
        while time.time() < deadline and armed(agent, session, token):
            if time.time() - renewed >= 1800:
                renewed = time.time()
                try:
                    report.report(agent, str(session), 'heartbeat', {'listening_until': round(deadline), 'lease_seconds': 3600})
                except Exception:
                    pass
            try:
                messages = claim(agent, session, wait=min(poll, max(1, deadline - time.time())),wake_path='claude-listener')
            except (OSError, ValueError):
                time.sleep(15)
                continue
            if messages:
                from .native_inbox import accepted_ids
                known=accepted_ids(agent,session,[m['id'] for m in messages])
                already=[m for m in messages if m['id'] in known]
                if already:
                    acknowledge_wake(agent,session,already)
                messages=[m for m in messages if m['id'] not in known]
                if not messages:continue
                cancel_listener(agent, session)
                accepted_ids(agent,session,[m['id'] for m in messages],remember=True)
                print(delivery(messages), file=sys.stderr)
                try:acknowledge_wake(agent,session,messages)
                except (OSError,ValueError):pass
                return 2
        return 0
    except Exception:
        return 0


def queue_delivery(agent, session, messages):
    """Hand dashboard input to the agent's own durable queue; True when accepted."""
    path = QUEUE_AGENTS.get(agent)
    if not path:
        return False
    import shutil
    binary = report.setting('AXON_CODEX_BIN') or shutil.which('codex') or str(Path.home()/'.local/bin/codex')
    for message in messages:
        try:
            done = subprocess.run([binary, 'queue', '--thread', str(session),
                                   '--message', message['text']], capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return False
        if done.returncode != 0:
            return False
    return True


def queue_listen(agent):
    """Detached idle watcher: wake claims for this session go to the agent's queue."""
    try:
        data = json.load(sys.stdin)
        session = data.get('session_id') if isinstance(data, dict) else None
        path = QUEUE_AGENTS.get(agent)
        seconds = min(LISTEN_CAP, max(0, int(report.setting('AXON_LISTEN_SECONDS', '10800'))))
        if session and agent in QUEUE_AGENTS and not wake_enabled(agent):
            cancel_listener(agent, session)
        if not session or not path or not seconds or not wake_enabled(agent):
            return 0
        poll = min(50, max(1, int(report.setting('AXON_LISTEN_POLL', '50'))))
        token = arm_listener(agent, session)
        deadline, renewed = time.time() + seconds, 0
        while time.time() < deadline and armed(agent, session, token):
            if time.time() - renewed >= 1800:
                renewed = time.time()
                try:
                    report.report(agent, str(session), 'heartbeat', {'queue_watching_until': round(deadline), 'lease_seconds': 3600})
                except Exception:
                    pass
            try:
                messages = claim(agent, session, wait=min(poll, max(1, deadline - time.time())), wake_path=path)
            except (OSError, ValueError):
                time.sleep(15)
                continue
            if not messages:
                continue
            from .native_inbox import accepted_ids
            known = accepted_ids(agent, session, [m['id'] for m in messages])
            already = [m for m in messages if m['id'] in known]
            if already:
                acknowledge_wake(agent, session, already)
            messages = [m for m in messages if m['id'] not in known]
            if not messages:
                continue
            # Accepted input becomes Codex's responsibility (its queue is durable
            # across clients); a failed call releases the lease for a later retry.
            accepted = queue_delivery(agent, session, messages)
            if accepted:
                accepted_ids(agent, session, [m['id'] for m in messages], remember=True)
            try:
                acknowledge_wake(agent, session, messages, accepted)
            except (OSError, ValueError):
                pass
            if not accepted:
                time.sleep(5)
        return 0
    except Exception:
        return 0


def spawn_queue_listener(agent, session):
    """Detach one watcher per session from the Stop hook; earlier watchers notice
    the new registration or a cancel and exit on their own poll."""
    try:
        here = Path(__file__).resolve()
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(
            str(p) for p in (here.parents[1], here.parents[2] / 'axon_protocol') if p.is_dir()))
        child = subprocess.Popen([sys.executable, '-m', 'axon_reporter.plugin_runtime',
                                  'queue-listen', agent], stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True, env=env)
        if child.stdin is not None:
            child.stdin.write(json.dumps({'session_id': session}).encode())
            child.stdin.close()
    except OSError:
        pass


def claude_agent_status(session):
    """True/False from `claude agents --json` for this native session id; None if unknowable."""
    import shutil
    binary = report.setting('AXON_CLAUDE_BIN') or shutil.which('claude')
    if not binary:
        return None
    try:
        done = subprocess.run([binary, 'agents', '--json'], capture_output=True, text=True, timeout=10,
                              stdin=subprocess.DEVNULL)
        if done.returncode != 0:
            return None
        agents = json.loads(done.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    entries = agents.get('agents') if isinstance(agents, dict) else agents
    if not isinstance(entries, list):
        return None
    return any(isinstance(e, dict) and e.get('sessionId') == session for e in entries)


def liveness_listen(agent):
    """Detached watcher for a claude session Stop fired on: keep the lease fresh while
    `claude agents --json` still lists it, and report the exit once it stops listing it."""
    try:
        data = json.load(sys.stdin)
        session = data.get('session_id') if isinstance(data, dict) else None
        seconds = min(LISTEN_CAP, max(0, int(report.setting('AXON_LISTEN_SECONDS', '10800'))))
        interval = min(600, max(30, int(report.setting('AXON_LIVENESS_POLL', '60'))))
        if not session or agent != 'claude' or not seconds:
            return 0
        # An older server would strip the unknown field, leaving bare heartbeats that
        # renew the lease forever: only watch hosts whose journal understands liveness.
        if 'liveness' not in (report.server_fields() or set()):
            return 0
        deadline = time.time() + seconds
        while time.time() < deadline:
            status = claude_agent_status(str(session))
            if status is False:
                report.report(agent, str(session), 'heartbeat', {'liveness': 'exited', 'lease_seconds': 3600})
                return 0
            if status is True:
                report.report(agent, str(session), 'heartbeat', {'liveness': 'alive', 'lease_seconds': 3600})
            # None: the listing failed (claude absent or busy); keep waiting, never guess.
            time.sleep(min(interval, max(1, deadline - time.time())))
        return 0
    except Exception:
        return 0


def spawn_liveness_watcher(agent, session):
    """Detach one liveness watcher per claude session from the Stop hook."""
    try:
        here = Path(__file__).resolve()
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(
            str(p) for p in (here.parents[1], here.parents[2] / 'axon_protocol') if p.is_dir()))
        child = subprocess.Popen([sys.executable, '-m', 'axon_reporter.plugin_runtime',
                                  'liveness', agent], stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True, env=env)
        if child.stdin is not None:
            child.stdin.write(json.dumps({'session_id': session}).encode())
            child.stdin.close()
    except OSError:
        pass


def run_hook(agent):
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            return
        # Context is still provided if the broker or local outbox is unavailable.
        try:
            lifecycle(agent,data)
        except Exception:
            pass
        event, session = data.get('hook_event_name'), data.get('session_id')
        notes = []
        if event in ('SessionStart','UserPromptSubmit') and session:
            notes.append(context(agent,session))
        if agent in INBOX_AGENTS and session:
            # Local input or exit ends the idle listener; in-turn input arrives as context.
            if event in ('UserPromptSubmit','SessionEnd'):
                cancel_listener(agent,session)
            if event in ('UserPromptSubmit','PostToolUse') and (event == 'UserPromptSubmit' or inbox_due(agent,session)):
                try:
                    messages = claim(agent,session,timeout=.5)
                except Exception:
                    messages = []
                if messages:
                    notes.append(delivery(messages))
            # Codex keeps going when Stop blocks, taking the reason as its next prompt, so input
            # that arrived after the last tool call is answered now instead of waiting for a user.
            if agent == 'codex' and event == 'Stop':
                try:
                    messages = claim(agent,session,timeout=.5)
                except Exception:
                    messages = []
                if messages:
                    print(json.dumps({'decision':'block','reason':delivery(messages)}))
                    return
                # The turn is over and nothing pending: watch for dashboard input while
                # the thread idles, and hand whatever arrives to Codex's own queue.
                if wake_enabled(agent):
                    spawn_queue_listener(agent, session)
                    report.report(agent, str(session), 'heartbeat', {'queue_watching': True})
            # Claude has no durable queue, but `claude agents --json` is a real liveness
            # registry: watch the idle session there so the board says alive/exited
            # instead of guessing lost contact when the terminal goes away.
            if agent == 'claude' and event == 'Stop' and wake_enabled(agent):
                spawn_liveness_watcher(agent, session)
        if notes:
            print(json.dumps({'hookSpecificOutput':{'hookEventName':event,'additionalContext':'\n\n'.join(notes)}}))
    except Exception:
        pass  # no decisions, stderr, or nonzero exits from a reporting hook


if __name__ == '__main__':
    if sys.argv[1:2] == ['queue-listen'] and len(sys.argv) == 3:
        sys.exit(queue_listen(sys.argv[2]))
    if sys.argv[1:2] == ['liveness'] and len(sys.argv) == 3:
        sys.exit(liveness_listen(sys.argv[2]))
    sys.exit(2)
