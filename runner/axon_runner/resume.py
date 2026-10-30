"""Resume sessions nothing on this host is running, when dashboard input arrives (#18).

The journal hands this runner the pending input of a session on this machine
that has wake consent and no live listener. The session travels as Axon's hashed
key only; this module finds the agent's own session id and folder in the agent's
local store, checks the agent's own liveness, and continues the session with the
agent's native resume. Nothing about the session leaves the host but the outcome."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sqlite3
import time
import urllib.parse

from . import config

RESUME_TIMEOUT = 90
UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


class QueuedWakeFailed(Exception):
    """Codex accepted durable input, but its separate resume request failed."""


class Refused(Exception):
    """A resume that did not happen; the message is a reason code the board shows."""


def session_key(native):
    """The reporter's hashed session key (axon_reporter.report.identity)."""
    return hashlib.sha256(native.encode()).hexdigest()[:24]


def delivery(messages):
    """Same framing the in-session listeners use, so a resumed turn reads alike."""
    lines = [time.strftime('[%H:%M] ', time.localtime(m['at'])) + m['text'] for m in messages]
    return ('The user sent this to this session from the Axon dashboard. '
            'It is the user\'s own input; respond to it as you would a typed message:\n' + '\n'.join(lines))


def first_cwd(path, limit=200):
    """The working folder recorded near the top of a transcript, or None."""
    try:
        with path.open(encoding='utf-8', errors='replace') as lines:
            for _, line in zip(range(limit), lines):
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                cwd = entry.get('cwd') or (entry.get('payload') or {}).get('cwd')
                if isinstance(cwd, str) and cwd:
                    return Path(cwd)
    except OSError:
        return None
    return None


def claude_home():
    return Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')


def codex_home():
    return Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')


def find_claude(key):
    """(session id, cwd) for a Claude transcript whose id hashes to key."""
    for path in (claude_home() / 'projects').glob('*/*.jsonl'):
        if UUID.fullmatch(path.stem) and session_key(path.stem) == key:
            return path.stem, first_cwd(path)
    return None, None


def find_codex(key):
    """(thread id, cwd) for a Codex rollout whose thread id hashes to key."""
    for path in (codex_home() / 'sessions').rglob('rollout-*.jsonl'):
        thread = path.stem[-36:]
        if UUID.fullmatch(thread) and session_key(thread) == key:
            return thread, first_cwd(path)
    return None, None


def claude_alive(executable, session):
    """True/False from `claude agents --json`; an open session must never be forked."""
    try:
        done = subprocess.run([executable, 'agents', '--json'], capture_output=True, text=True,
                              timeout=15, stdin=subprocess.DEVNULL)
        listing = json.loads(done.stdout) if done.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        listing = None
    entries = listing.get('agents') if isinstance(listing, dict) else listing
    if not isinstance(entries, list):
        raise Refused('liveness_unknown')
    return any(isinstance(e, dict) and e.get('sessionId') == session for e in entries)


def codex_loaded(thread):
    """True when a client holds the thread's writer lock (Codex flocks it while loaded)."""
    try:
        fd = os.open(codex_home() / 'thread-writer-locks' / (thread + '.lock'), os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def tail_of(text, code):
    # The journal only takes printable ASCII reasons; anything else would bounce the ack.
    # Folder paths stay on the host, so they never ride along in a reason.
    text = re.sub(r'[^ -~]+', ' ', text or '')
    text = re.sub(r'(~|\.{0,2})/[^\s`\'"]*', '<path>', text).strip()[-150:]
    return text or 'exit %d' % code


def resume_claude(key, messages, settings, executable=None):
    executable = executable or 'claude'
    mode = settings['permission_mode']
    if mode not in config.PERMISSION_MODES:
        raise Refused('bad_permission_mode')
    session, cwd = find_claude(key)
    if not session:
        raise Refused('session_not_found')
    if not cwd or not cwd.is_dir():
        raise Refused('project_path_missing')
    if claude_alive(executable, session):
        # Open but not listening (wake off on this host, or the listener lapsed):
        # `--resume` would start a copy beside it, so leave it to the person there.
        raise Refused('session_open')
    command = [executable, '--bg', '--resume', session, '--permission-mode', mode, delivery(messages)]
    try:
        done = subprocess.run(command, cwd=str(cwd), capture_output=True, text=True,
                              timeout=RESUME_TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise Refused('launch_timeout') from None
    except OSError as error:
        raise Refused('launch_failed: %s' % type(error).__name__) from None
    if done.returncode:
        raise Refused('launch_failed: ' + tail_of(done.stderr or done.stdout, done.returncode))
    return 'resumed in the background'


def queue_receipts(key, messages, log_dir, warning=None, remember=False):
    """Local IDs-only receipts survive an acknowledgment retry without another enqueue."""
    path=(log_dir or Path('.'))/'codex-queue-receipts.sqlite3'
    fd=os.open(path,os.O_CREAT|os.O_WRONLY,0o600);os.close(fd)
    with sqlite3.connect(path,timeout=2) as db:
        db.execute('CREATE TABLE IF NOT EXISTS receipts(session TEXT,id TEXT,warning TEXT,PRIMARY KEY(session,id))')
        if remember:
            db.executemany('INSERT OR REPLACE INTO receipts VALUES(?,?,?)',[(key,m['id'],warning) for m in messages])
        return {row[0]:row[1] for row in db.execute('SELECT id,warning FROM receipts WHERE session=?',(key,))}


def resume_codex(key, messages, settings, executable=None, log_dir=None):
    executable = executable or 'codex'
    sandbox = settings['sandbox']
    if sandbox not in config.SANDBOXES:
        raise Refused('bad_sandbox')
    thread, cwd = find_codex(key)
    if not thread:
        raise Refused('session_not_found')
    if not cwd or not cwd.is_dir():
        raise Refused('project_path_missing')
    receipts=queue_receipts(key,messages,log_dir)
    pending=[m for m in messages if m['id'] not in receipts]
    prior_warning=any(receipts.get(m['id']) for m in messages)
    if not pending:
        if prior_warning: raise QueuedWakeFailed
        return 'already accepted by Codex'
    prompt=delivery(pending)
    if codex_loaded(thread):
        # A client has it open: its own durable queue makes this the next turn there.
        try:
            done = subprocess.run([executable, 'queue', '--thread', thread, '--message', prompt, '--resume'],
                                  capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            raise Refused('queue_failed') from None
        if done.returncode:
            raise Refused('queue_failed: ' + tail_of(done.stderr or done.stdout, done.returncode))
        warning='codex_queue_wake_failed' if 'could not resume thread' in (done.stderr or '').lower() else None
        queue_receipts(key,pending,log_dir,warning,remember=True)
        if warning or prior_warning:
            raise QueuedWakeFailed
        return 'queued and resume requested for the open thread'
    # `exec resume` also drains anything already in the thread's Codex queue into this turn.
    command = [executable, 'exec', 'resume', '--json', '-c', 'sandbox_mode="%s"' % sandbox, thread, prompt]
    log_path = (log_dir or Path('.')) / ('resume-%s.log' % key[:8])
    try:
        with log_path.open('a', encoding='utf-8', errors='replace') as log_file:
            proc = subprocess.Popen(command, cwd=str(cwd), stdin=subprocess.DEVNULL,
                                    stdout=log_file, stderr=subprocess.STDOUT)
    except OSError as error:
        raise Refused('launch_failed: %s' % type(error).__name__) from None
    deadline = time.time() + RESUME_TIMEOUT
    while time.time() < deadline:
        text = log_path.read_text(errors='replace') if log_path.exists() else ''
        if '"turn.started"' in text:
            return 'resumed headless'
        if proc.poll() is not None:
            if proc.returncode == 0:
                return 'resumed headless'
            raise Refused('launch_failed: ' + tail_of(text.splitlines()[-1] if text else '', proc.returncode))
        time.sleep(0.05)
    proc.kill()
    raise Refused('launch_timeout')


RESUMERS = {'claude': resume_claude, 'codex': resume_codex}


def run_once(server, machine, credential, policy, wait, post, log=print, log_dir=None):
    """Claim one dormant session's input, resume it, and acknowledge the outcome."""
    base = server.rstrip('/')
    agents = ','.join(policy.resume['agents'])
    try:
        reply = post('%s/hosts/%s/resume/claim?%s' % (base, urllib.parse.quote(machine),
                                                      urllib.parse.urlencode({'wait': wait, 'agents': agents})),
                     credential=credential, timeout=wait + 15)
    except OSError as error:
        log('server unavailable: %s' % type(error).__name__)
        return 'unavailable'
    claim = reply.get('resume')
    if not claim:
        return None
    agent, key, messages = claim['agent'], claim['session'], claim['messages']
    warning = None
    resumer = RESUMERS.get(agent)
    try:
        if resumer is None or agent not in policy.resume['agents']:
            raise Refused('not_allowed')
        kwargs = {'log_dir': log_dir} if agent == 'codex' else {}
        detail, refusal = resumer(key, messages, policy.resume, policy.executable.get(agent), **kwargs), None
    except QueuedWakeFailed:
        detail, refusal, warning = 'queued in Codex; automatic wake failed', None, 'codex_queue_wake_failed'
    except Refused as error:
        detail, refusal = None, str(error)
    payload = {'ids': [m['id'] for m in messages], 'claim_token': messages[0]['claim_token'],
               'accepted': refusal is None}
    if refusal:
        payload['error'] = refusal
    if warning:
        payload['warning'] = warning
    try:
        post('%s/sessions/%s/%s/inbox/wake/ack' % (base, urllib.parse.quote(claim['source']), key),
             payload=payload, credential=credential)
    except OSError as error:
        log('could not acknowledge resume: %s' % type(error).__name__)
    log('resume %s/%s %s' % (agent, key[:8], refusal and 'refused: ' + refusal or detail))
    return claim
