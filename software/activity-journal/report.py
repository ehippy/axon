#!/usr/bin/env python3
"""Publish explicit milestones or safe lifecycle facts to Axon, with a local retry outbox."""
from contextlib import closing
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import time
import uuid


def identity(agent, raw_session):
    machine = socket.gethostname().lower().split('.')[0]
    source = re.sub(r'[^a-z0-9_.-]', '-', socket.gethostname().lower() if agent == 'claude' else 'hermes' if agent == 'hermes' else agent.lower() + '-' + machine)[:128]
    session = hashlib.sha256(raw_session.encode()).hexdigest()[:24]
    return source, session, machine


def spool_path():
    root = Path(os.environ.get('AXON_ACTIVITY_HOME', Path.home() / '.local/state/axon'))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root / 'outbox.sqlite3'


def report(agent, raw_session, kind, data, flush=True):
    if not raw_session:
        raise ValueError('a stable session identity is required')
    source, session, machine = identity(agent, raw_session)
    event = dict(version=1, source=source, session=session, id=uuid.uuid4().hex,
                 at=time.time(), kind=kind, data={'agent': agent, 'machine': machine, **data})
    # Share the collector's validation; a bad event must never poison the retry queue.
    try:
        from .journal import validate
    except ImportError:
        from journal import validate
    validate(event)
    path = spool_path()
    # Explicitly create the file privately before SQLite opens it.
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(path, timeout=0.2)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS outbox(seq INTEGER PRIMARY KEY, topic TEXT, payload TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS summaries(source TEXT, session TEXT, payload TEXT, at REAL, PRIMARY KEY(source,session))')
        prior=db.execute('SELECT payload FROM summaries WHERE source=? AND session=?',(source,session)).fetchone()
        snapshot=json.loads(prior[0]) if prior else {}
        previous=snapshot.get('state')
        if kind=='start':
            for key in ('goal','step','outcome','evidence','blocker'):
                snapshot.pop(key,None)
        snapshot.update(data)
        state={'start':'working','waiting':'waiting','blocked':'blocked','finished':'finished','stopped':'stopped','interrupted':'interrupted'}.get(kind)
        snapshot['state']='finished' if previous=='finished' and kind!='start' else state or snapshot.get('state','working')
        db.execute('INSERT OR REPLACE INTO summaries VALUES(?,?,?,?)',(source,session,json.dumps(snapshot),event['at']))
        db.execute('INSERT INTO outbox(topic,payload) VALUES(?,?)',
                   (f'axon/activity/{source}/{session}/events', json.dumps(event)))
    if flush:
        drain(path)
        publish_summary(path,source)
    return event


def publish_summary(path, source):
    """Retain the old machine-status contract for displays that haven't upgraded."""
    with closing(sqlite3.connect(path,timeout=.2)) as db:
        rows=db.execute('SELECT payload,at FROM summaries WHERE source=? AND at>? ORDER BY at DESC',
                        (source,time.time()-3600)).fetchall()
    if not rows:
        return
    active=[(json.loads(row[0]),row[1]) for row in rows]
    for snapshot,at in active:
        if snapshot.get('state') in ('working','waiting','blocked') and time.time()-at>snapshot.get('lease_seconds',900):
            snapshot['state']='lost_contact'
    running=[row for row in active if row[0].get('state')=='working']
    blocked=[row for row in active if row[0].get('state')=='blocked']
    snapshot,at=(blocked or running or active)[0]
    state='error' if blocked else 'running' if running else 'idle'
    task=snapshot.get('outcome') if snapshot.get('state')=='finished' else snapshot.get('goal') or snapshot.get('step') or snapshot.get('activity') or snapshot.get('state')
    if snapshot.get('project'):
        task=f"{snapshot['project']}: {task}"
    if len(running)>1:
        task=f'{len(running)} chats · {task}'
    binary=os.environ.get('AXON_MOSQUITTO_PUB') or shutil.which('mosquitto_pub')
    if not binary and Path('/opt/homebrew/bin/mosquitto_pub').exists():
        binary='/opt/homebrew/bin/mosquitto_pub'
    if binary:
        try:
            subprocess.run([binary,'-h',os.environ.get('AXON_BROKER','localhost'),
                '-p',os.environ.get('AXON_PORT','1883'),'-t',f'axon/agents/{source}/status',
                '-m',json.dumps({'state':state,'task':str(task)[:80],'ts':at}),'-r','-q','1'],
                timeout=.4,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        except (OSError,subprocess.TimeoutExpired):
            pass


def drain(path=None):
    path = path or spool_path()
    binary = os.environ.get('AXON_MOSQUITTO_PUB') or shutil.which('mosquitto_pub')
    if not binary and Path('/opt/homebrew/bin/mosquitto_pub').exists():
        binary = '/opt/homebrew/bin/mosquitto_pub'
    if not binary:
        return
    # A separate retry command/service can drain longer outages; hooks do bounded work.
    deadline = time.monotonic() + 1.2
    with closing(sqlite3.connect(path, timeout=0.2)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS outbox(seq INTEGER PRIMARY KEY, topic TEXT, payload TEXT)')
        for row in db.execute('SELECT seq,topic,payload FROM outbox ORDER BY seq LIMIT 20').fetchall():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                result = subprocess.run([binary, '-h', os.environ.get('AXON_BROKER', 'localhost'),
                        '-p', os.environ.get('AXON_PORT', '1883'), '-t', row[1], '-m', row[2], '-q', '1'], timeout=remaining,
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if result.returncode:
                    break
                db.execute('DELETE FROM outbox WHERE seq=?', (row[0],))
            except (OSError, subprocess.TimeoutExpired):
                break


def hook(data, agent):
    session = data.get('session_id')
    if not session:
        return
    event = data.get('hook_event_name')
    project = re.sub(r'[^\w .-]', '', Path(data.get('cwd') or '.').name)[:120]
    kind = {'SessionStart': 'waiting', 'UserPromptSubmit': 'start', 'PermissionRequest': 'waiting',
            'Stop': 'waiting', 'Interrupt': 'interrupted', 'SessionEnd': 'stopped',
            'PreToolUse': 'heartbeat', 'PostToolUse': 'heartbeat'}.get(event)
    if not kind:
        return
    patch = {'project': project} if data.get('cwd') else {}
    if event == 'PermissionRequest':
        patch['activity'] = 'needs approval'
    elif event in ('Stop', 'SessionStart'):
        patch['activity'] = 'awaiting input'
    elif event in ('PreToolUse', 'PostToolUse'):
        patch['state'] = 'working'
        tool = str(data.get('tool_name', '')).lower()
        patch['activity'] = ('editing' if any(x in tool for x in ('edit', 'write', 'patch')) else
                             'running tools' if any(x in tool for x in ('exec', 'bash', 'shell')) else
                             'researching' if any(x in tool for x in ('search', 'web', 'fetch')) else 'working')
    report(agent, session, kind, patch)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--agent', default=os.environ.get('AXON_AGENT', 'codex'))
    p.add_argument('--session', default=os.environ.get('AXON_SESSION_ID') or os.environ.get('CODEX_THREAD_ID'))
    sub = p.add_subparsers(dest='command', required=True)
    for kind in ('start', 'progress', 'heartbeat', 'waiting', 'blocked', 'finished', 'stopped', 'interrupted'):
        cmd = sub.add_parser(kind)
        for field in ('project', 'goal', 'step', 'outcome', 'branch', 'parent', 'blocker'):
            cmd.add_argument('--' + field)
        cmd.add_argument('--evidence', action='append')
        cmd.add_argument('--lease-seconds', type=int)
    sub.add_parser('hook')
    sub.add_parser('retry')
    args = p.parse_args()
    if args.command == 'retry':
        drain(); return
    if args.command == 'hook':
        try:
            hook(json.load(__import__('sys').stdin), args.agent)
        except Exception:
            pass  # hooks never interfere with the agent
        return
    if not args.session:
        p.error('pass --session or set AXON_SESSION_ID / CODEX_THREAD_ID')
    data = {key: value for key, value in vars(args).items() if key not in ('agent', 'session', 'command') and value is not None}
    try:
        event = report(args.agent, args.session, args.command, data)
    except ValueError as e:
        p.error(str(e))
    print(json.dumps({'queued': True, 'source': event['source'], 'session': event['session'], 'event': event['id']}))


if __name__ == '__main__':
    main()
