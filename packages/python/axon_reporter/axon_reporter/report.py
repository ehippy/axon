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
import shlex
import socket
import sqlite3
import subprocess
import time
import uuid
import sys

# Support the exact script command supplied to agents, in source and built bundles.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    source_protocol = Path(__file__).resolve().parents[2] / "axon_protocol"
    if source_protocol.is_dir():
        sys.path.insert(0, str(source_protocol))

from axon_protocol import MACHINE_KINDS, validate


def setting(name, default=None):
    """Read literal host-local settings without executing shell code; env wins."""
    if name in os.environ:
        return os.environ[name]
    path = Path(os.environ.get('AXON_CONFIG_FILE', Path.home()/'.config/axon/env'))
    try:
        for line in path.read_text().splitlines():
            words = shlex.split(line, comments=True)
            if words[:1] == ['export']:
                words = words[1:]
            if len(words) == 1 and words[0].startswith(name+'='):
                return words[0].split('=',1)[1]
    except (OSError, ValueError):
        pass
    return default


# Sentinel label: resolve the host name at report time instead of storing one.
MACHINE_ALIAS_AUTO = 'auto'


def detected_machine_alias():
    """Short host name reduced to a dashboard-safe label, or None when unusable."""
    try:
        name = socket.gethostname().split('.')[0]
    except OSError:
        return None
    name = re.sub(r'[^a-zA-Z0-9_.-]', '-', name)[:80].strip('-._')
    return name if re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}', name) else None


def machine_identity():
    """Host label: a literal AXON_MACHINE, 'auto' for the host name, else a private ID."""
    alias = setting('AXON_MACHINE')
    if alias is None:
        return installation_identity()
    if alias.strip().lower() == MACHINE_ALIAS_AUTO:
        # Opting in to host names must never stop reporting, so fall back silently.
        return detected_machine_alias() or installation_identity()
    if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,80}', alias):
        raise ValueError('AXON_MACHINE must be auto or 1..80 letters, digits, dots, underscores or hyphens')
    return alias


# SMBIOS chassis types; anything else (tablet, handheld, unknown) stays unreported.
DMI_KINDS = {**dict.fromkeys((3, 4, 5, 6, 7, 13, 15, 16, 24, 35, 36), 'desktop'),
             **dict.fromkeys((8, 9, 10, 14, 31, 32), 'laptop'),
             **dict.fromkeys((17, 23, 25, 28, 29), 'server')}
DEVICETREE_KINDS = {'laptop': 'laptop', 'convertible': 'laptop', 'desktop': 'desktop', 'server': 'server'}


def machine_kind():
    """Host icon class: explicit AXON_MACHINE_KIND, else detected hardware, else None."""
    kind = setting('AXON_MACHINE_KIND')
    if kind is not None:
        if kind not in MACHINE_KINDS:
            raise ValueError('AXON_MACHINE_KIND must be one of ' + ', '.join(sorted(MACHINE_KINDS)))
        return kind
    try:
        return detect_machine_kind()
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def detect_machine_kind():
    if sys.platform == 'darwin':
        result = subprocess.run(['/usr/bin/pmset', '-g', 'batt'], capture_output=True, text=True, timeout=1)
        if result.returncode:
            return None
        return 'laptop' if 'InternalBattery' in result.stdout else 'desktop'
    dmi = Path('/sys/class/dmi/id/chassis_type')
    if dmi.exists() and (kind := DMI_KINDS.get(int(dmi.read_text().strip()))):
        return kind
    tree = Path('/sys/firmware/devicetree/base/chassis-type')
    if tree.exists() and (kind := DEVICETREE_KINDS.get(tree.read_bytes().strip(b'\0\n').decode())):
        return kind
    # A system battery (not a wireless peripheral's) means a portable machine.
    for supply in Path('/sys/class/power_supply').glob('*'):
        scope = supply/'scope'
        if (supply/'type').read_text().strip() == 'Battery' and not (scope.exists() and scope.read_text().strip() == 'Device'):
            return 'laptop'
    return None


def installation_identity():
    path = spool_path().with_name('identity.sqlite3')
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(path, timeout=1)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS identity(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        db.execute('INSERT OR IGNORE INTO identity VALUES(?,?)', ('machine', uuid.uuid4().hex[:16]))
        return db.execute('SELECT value FROM identity WHERE key=?', ('machine',)).fetchone()[0]


def identity(agent, raw_session):
    machine = machine_identity()
    source = setting('AXON_SOURCE') or agent.lower() + '-' + installation_identity()
    if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,128}', source):
        raise ValueError('AXON_SOURCE must contain 1..128 letters, digits, dots, underscores or hyphens')
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
    if (chassis := machine_kind()):
        event['data']['machine_kind'] = chassis
    validate(event)
    path = spool_path()
    # Explicitly create the file privately before SQLite opens it.
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(path, timeout=0.2)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS outbox(seq INTEGER PRIMARY KEY, topic TEXT, payload TEXT)')
        db.execute('INSERT INTO outbox(topic,payload) VALUES(?,?)',
                   (f'axon/activity/{source}/{session}/events', json.dumps(event)))
    if flush:
        drain(path)
    return event


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
                result = subprocess.run([binary, '-h', setting('AXON_BROKER', 'localhost'),
                        '-p', setting('AXON_PORT', '1883'), '-t', row[1], '-m', row[2], '-q', '1'], timeout=remaining,
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
    if event == 'UserPromptSubmit':
        patch['user_turn'] = True
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
