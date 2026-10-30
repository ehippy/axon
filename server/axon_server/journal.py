#!/usr/bin/env python3
"""Axon's durable session journal. Standard library + Mosquitto CLI only."""
from contextlib import contextmanager
from importlib.resources import files
import argparse
import hmac
import hashlib
import json
import logging
import re
import os
import select
import secrets
import signal
import socket
import sqlite3
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, unquote
from urllib.request import urlopen

from axon_protocol import STATES, FIELDS, TERMINAL, capabilities, known_data, validate
from axon_protocol.resources import parse_inference_metrics, validate_resource
from axon_server.projects import ProjectStore

# How long an auto-approved permission prompt stays invisible before the board admits
# the session may actually be stuck (#26: auto-approvals resolved in 20-40s, human gates wait indefinitely).
APPROVAL_BEAT = 60

IDENTITY = re.compile(r'[a-zA-Z0-9_.-]{1,128}')
INFERENCE_LABEL = re.compile(r'[a-zA-Z0-9_.-]{1,80}')
MESSAGE_LIMIT = 4000
CLAIM_WAIT_LIMIT = 55
STREAM_PING = 15
UPDATE_UNIT = 'axon-update.service'
UPDATE_DEBOUNCE = 30
WAKE_PATHS = {'pi-native':'pi', 'hermes-native':'hermes', 'opencode-native':'opencode',
              'codex-queue':'codex', 'claude-resume':'claude', 'claude-listener':'claude',
              'codex-resume':'codex'}
WAKE_LEASE = 90
# Host runners resume sessions nothing on the host is running (#18): input for a
# session with wake consent whose process is gone is handed to that host's runner.
RESUME_PATHS = {'claude': 'claude-resume', 'codex': 'codex-resume'}
# An armed in-session listener takes input within a second; the runner only sees
# input older than this, so a live session always wins the race.
RESUME_GRACE = 10
# A refused resume (session still open, CLI failed) stays visible and is not
# retried until the next message or this long, so a runner never spins on it.
RESUME_HOLD = 600
RESUME_REASON = re.compile(r'[a-z_]{1,40}(: [ -~]{1,160})?')

# Start-job queue for host runners (#19). Projects travel as bare names only:
# folder paths stay host-local and never cross the wire.
MACHINE = re.compile(r'[a-zA-Z0-9_.-]{1,80}')
JOB_AGENTS = {'claude', 'codex', 'pi', 'opencode', 'hermes'}
JOB_STATES = {'queued': {'claimed'}, 'claimed': {'started', 'refused'},
              'started': {'finished', 'failed'}}
JOB_REPORTS = {'started', 'refused', 'failed', 'finished'}
JOB_DETAIL_LIMIT = 512
JOB_LIST_LIMIT = 100
# A runner names at most this many startable projects; the board offers them.
START_LIMIT = 64


WEB_ASSETS = {'/': ('index.html', 'text/html; charset=utf-8'),
                          '/ui/app.css': ('app.css', 'text/css; charset=utf-8'),
                          '/ui/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                          '/ui/favicon.svg': ('favicon.svg', 'image/svg+xml'),
                          '/ui/apple-touch-icon.png': ('apple-touch-icon.png', 'image/png'),
                          **{f'/ui/icons/{name}.svg': (f'icons/{name}.svg', 'image/svg+xml')
                             for name in ('laptop', 'pc-display', 'hdd-rack', 'chat-dots')}}


def server_revision():
    """Fingerprint code and shipped UI, independent of data and deployment paths."""
    digest = hashlib.sha256(Path(__file__).read_bytes())
    digest.update(Path(__file__).with_name('projects.py').read_bytes())
    for name in sorted({asset[0] for asset in WEB_ASSETS.values()}):
        digest.update(name.encode())
        digest.update(files('axon_server').joinpath('web', *name.split('/')).read_bytes())
    return digest.hexdigest()


class Journal(ProjectStore):
    def __init__(self, path, retention_days=30):
        if type(retention_days) is not int or retention_days < 0:
            raise ValueError('retention_days must be a nonnegative integer')
        self.retention_days = retention_days
        self.path = str(path)
        self.inbox = threading.Condition()
        self.changes = threading.Condition()
        self.version = 0
        self._work_lock = threading.Lock()
        with self.connect() as db:
            db.executescript('''
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, session TEXT, id TEXT,
                at REAL, received REAL, kind TEXT, payload TEXT,
                UNIQUE(source, session, id));
              CREATE INDEX IF NOT EXISTS event_session ON events(source, session, seq);
              CREATE TABLE IF NOT EXISTS sessions (
                source TEXT, session TEXT, at REAL, payload TEXT,
                PRIMARY KEY(source, session));
              CREATE TABLE IF NOT EXISTS resources (
                provider TEXT, account TEXT, observed_at REAL, payload TEXT,
                PRIMARY KEY(provider, account));
              CREATE TABLE IF NOT EXISTS session_archives (
                source TEXT, session TEXT, archived_at REAL NOT NULL,
                PRIMARY KEY(source, session));
              CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY, source TEXT, session TEXT, at REAL, text TEXT,
                delivered_at REAL);
              CREATE TABLE IF NOT EXISTS message_wake_warnings (
                message_id TEXT PRIMARY KEY, warning TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS message_session ON messages(source, session, at);
              CREATE TABLE IF NOT EXISTS message_claims (
                message_id TEXT PRIMARY KEY, token TEXT, until_at REAL, path TEXT);
              CREATE TABLE IF NOT EXISTS host_credentials (
                digest TEXT PRIMARY KEY, machine TEXT NOT NULL, created_at REAL NOT NULL,
                revoked_at REAL);
              CREATE TABLE IF NOT EXISTS session_wake (
                source TEXT, session TEXT, enabled INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(source, session));
              CREATE TABLE IF NOT EXISTS host_sync (
                machine TEXT PRIMARY KEY, at REAL, synced_revision TEXT,
                ok INTEGER, error TEXT);
              CREATE TABLE IF NOT EXISTS host_jobs (
                id TEXT PRIMARY KEY, machine TEXT NOT NULL, at REAL NOT NULL,
                agent TEXT NOT NULL, project TEXT NOT NULL, prompt TEXT NOT NULL,
                worktree INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'queued',
                detail TEXT, claimed_at REAL, ended_at REAL);
              CREATE INDEX IF NOT EXISTS host_job_queue ON host_jobs(machine, status, at);
              CREATE TABLE IF NOT EXISTS resume_holds (
                source TEXT, session TEXT, until_at REAL NOT NULL, reason TEXT NOT NULL,
                PRIMARY KEY(source, session));
              CREATE TABLE IF NOT EXISTS host_runners (
                machine TEXT PRIMARY KEY, seen_at REAL NOT NULL, resume_agents TEXT NOT NULL,
                starts TEXT NOT NULL DEFAULT '[]');
            ''')
            if 'starts' not in {r['name'] for r in db.execute('PRAGMA table_info(host_runners)')}:
                db.execute("ALTER TABLE host_runners ADD COLUMN starts TEXT NOT NULL DEFAULT '[]'")


            # Old databases used rowids that can be reused after deletion. Preserve
            # the event cursor high-water mark even when retention empties history.
            schema = db.execute("SELECT sql FROM sqlite_master WHERE name='events'").fetchone()[0]
            if 'AUTOINCREMENT' not in schema.upper():
                db.executescript('''
                  BEGIN IMMEDIATE;
                  CREATE TABLE events_retention (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, session TEXT, id TEXT,
                    at REAL, received REAL, kind TEXT, payload TEXT, UNIQUE(source,session,id));
                  INSERT INTO events_retention SELECT * FROM events ORDER BY seq;
                  DROP TABLE events;
                  ALTER TABLE events_retention RENAME TO events;
                  CREATE INDEX event_session ON events(source,session,seq);
                  COMMIT;
                ''')
            db.execute('CREATE INDEX IF NOT EXISTS event_age ON events(at)')
            db.execute('CREATE INDEX IF NOT EXISTS session_age ON sessions(at)')
        self.init_projects()
        self.prune()

    def prune(self, now=None):
        if not self.retention_days:
            return {'events': 0, 'sessions': 0}
        now = time.time() if now is None else now
        cutoff = now - self.retention_days * 86400
        with self.connect() as db:
            events = db.execute('DELETE FROM events WHERE at < ?', (cutoff,)).rowcount
            sessions = db.execute('DELETE FROM sessions WHERE at < ?', (cutoff,)).rowcount
            db.execute('DELETE FROM messages WHERE at < ?', (cutoff,))
            db.execute('DELETE FROM message_wake_warnings WHERE message_id NOT IN (SELECT id FROM messages)')
            db.execute('DELETE FROM message_claims WHERE NOT EXISTS (SELECT 1 FROM messages WHERE messages.id=message_claims.message_id)')
            db.execute('DELETE FROM session_archives WHERE NOT EXISTS (SELECT 1 FROM sessions s WHERE s.source=session_archives.source AND s.session=session_archives.session)')
            db.execute('DELETE FROM host_jobs WHERE ended_at IS NOT NULL AND ended_at < ?', (cutoff,))
        if events or sessions:
            with self._work_lock:
                self._work_version = None
                self._work_cursor = 0
                self._work_reports = {}
            self.changed()
        return {'events': events, 'sessions': sessions}


    def changed(self):
        """Wake live browser streams; they refetch, so the version is only a signal."""
        with self.changes:
            self.version += 1
            self.changes.notify_all()

    def wait_change(self, seen, timeout):
        with self.changes:
            self.changes.wait_for(lambda: self.version != seen, timeout)
            return self.version

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def ingest(self, event):
        stored = self._ingest(event)
        if stored:
            self.changed()
        return stored

    def _ingest(self, event):
        v = validate(event)
        # A delayed outbox or broker replay must not resurrect expired history.
        if self.retention_days and v['at'] < time.time() - self.retention_days * 86400:
            return False
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT * FROM sessions WHERE source=? AND session=?', (v['source'], v['session'])).fetchone()
            patch = v.get('data', {})
            # Completion is an explicit claim with evidence, never inferred from a Stop hook.
            if v['kind'] == 'finished' or patch.get('state') == 'finished':
                if not patch.get('outcome', '').strip() or not patch.get('evidence'):
                    raise ValueError('finished requires outcome and evidence')
            inserted = db.execute('INSERT OR IGNORE INTO events(source,session,id,at,received,kind,payload) VALUES(?,?,?,?,?,?,?)',
                       (v['source'], v['session'], v['id'], v['at'], time.time(), v['kind'], json.dumps(v))).rowcount
            if not inserted:
                return False
            if prior and v['at'] < prior['at']:
                return True  # historical arrival must not roll current state backwards
            current = json.loads(prior['payload']) if prior else {'source': v['source'], 'session': v['session']}
            if v['kind'] == 'start':
                for key in ('goal', 'step', 'outcome', 'evidence', 'blocker'):
                    current.pop(key, None)
            # A fresh user turn may restore a session; replays and background activity cannot.
            if patch.get('user_turn') is True:
                db.execute('DELETE FROM session_archives WHERE source=? AND session=? AND archived_at<?',
                           (v['source'], v['session'], v['at']))
            # Fields from a newer reporter stay in the stored event but never reach session state.
            current.update({k: value for k, value in known_data(patch).items() if k != 'user_turn'})
            if 'liveness' in patch:
                # Stamp the arrival so a later event (a resume) clears the exit reading.
                current['liveness_at'] = v['at']
            # A raised permission prompt is pending until any newer event proves it resolved
            # (auto-approver clears, denial, or the next tool). Other events clear the flag.
            if v['kind'] == 'waiting' and patch.get('activity') == 'needs approval':
                current['approval_pending'] = True
            else:
                current.pop('approval_pending', None)
            state = {'start': 'working', 'waiting': 'waiting', 'blocked': 'blocked',
                     'finished': 'finished', 'stopped': 'stopped', 'interrupted': 'interrupted'}.get(v['kind'])
            # Generic hooks preserve completion; explicit work updates can reopen it.
            substantive = v['kind'] in ('blocked', 'start', 'finished') or (
                v['kind'] in ('waiting', 'progress') and bool(patch.get('step')))
            if substantive and v['kind'] == 'progress':
                state = 'working'
            if prior and json.loads(prior['payload']).get('state') == 'finished' and not substantive:
                current['state'] = 'finished'
            elif state:
                current['state'] = state
            current.setdefault('state', 'working')
            current.setdefault('lease_seconds', 900)
            current['updated_at'] = v['at']
            db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?,?,?)', (v['source'], v['session'], v['at'], json.dumps(current)))
            self.project_seen(db,current,v['at'])
        return True

    def work_reports(self):
        """Project explicit work reports independently of lifecycle hooks, including old data."""
        with self._work_lock:
            return self._project_work_reports()

    def _project_work_reports(self):
        if getattr(self, '_work_version', None) == self.version:
            return self._work_reports
        version = self.version
        reports = dict(getattr(self, '_work_reports', {}))
        cursor = getattr(self, '_work_cursor', 0)
        with self.connect() as db:
            db.execute('BEGIN')
            newest = db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
            rows = db.execute("""SELECT source,session,kind,at,payload FROM events
                WHERE seq>? AND seq<=? AND (kind IN ('start','finished','blocked','progress') OR
                (kind='waiting' AND (json_extract(payload,'$.data.step') IS NOT NULL OR
                    (json_extract(payload,'$.data.activity')='needs approval' AND
                     json_extract(payload,'$.data.approval_gate')='human'))))
                ORDER BY at,seq""", (cursor, newest)).fetchall()
        for row in rows:
            data = json.loads(row['payload']).get('data', {})
            kind = row['kind']
            identity = (row['source'], row['session'])
            if row['at'] < reports.get(identity, {}).get('work_at', 0):
                continue
            if kind == 'progress' and not data.get('step'):
                # A subsequent tool proves a permission prompt was resolved. It says
                # nothing about an explicit request for a decision or operator action.
                if reports.get(identity, {}).get('attention', {}).get('kind') != 'approval':
                    continue
            human_approval = (kind == 'waiting' and data.get('activity') == 'needs approval'
                              and data.get('approval_gate') == 'human' and not data.get('step'))
            report = {'work_state': {'start':'working','progress':'working','waiting':'waiting',
                                    'blocked':'blocked','finished':'finished'}[kind],
                      'work_at': row['at']}
            if kind in ('waiting','blocked'):
                report['attention'] = {'text': data.get('blocker') or data.get('step') or ('Needs your approval in the agent client' if human_approval else 'Blocked'),
                                       'at': row['at'], 'kind': 'approval' if human_approval else kind}
            reports[(row['source'], row['session'])] = report
        self._work_reports = reports
        self._work_version = version
        self._work_cursor = newest
        return reports

    def sessions(self, filters=None):
        filters = filters or {}
        with self.connect() as db:
            rows = db.execute('SELECT s.payload, a.archived_at FROM sessions s LEFT JOIN session_archives a ON s.source=a.source AND s.session=a.session ORDER BY s.at DESC').fetchall()
            wake = {(r['source'], r['session']): bool(r['enabled']) for r in db.execute('SELECT * FROM session_wake')}
            holds = {(r['source'], r['session']): {'until': r['until_at'], 'reason': r['reason']}
                     for r in db.execute('SELECT * FROM resume_holds WHERE until_at>?', (time.time(),))}
            last_wakes = {(r['source'],r['session']):{'at':r['at'],'activity':json.loads(r['payload']).get('data',{}).get('activity','Dashboard delivery')}
                for r in db.execute("SELECT * FROM events WHERE id LIKE 'wake-%' ORDER BY at")}
        result = []
        reports = self.work_reports()
        overrides = self.project_overrides()
        names = self.project_names()
        for row in rows:
            item = json.loads(row[0])
            report = reports.get((item['source'], item['session']), {})
            item.update(report)
            # A later explicit report supersedes completion, even without another start.
            if item.get('state') == 'finished' and report.get('work_state', 'finished') != 'finished':
                item['state'] = report['work_state']
            if report and report.get('work_state') != 'finished' and item.get('outcome'):
                item['previous_outcome'] = item.pop('outcome')
                item['previous_evidence'] = item.pop('evidence', [])
            label = item.get('project')
            item = self.apply_project_override(item, overrides, names)
            item['wake'] = wake.get((item['source'], item['session']), False)
            if (last_wake:=last_wakes.get((item['source'],item['session']))):
                item['last_wake']=last_wake
            if (hold := holds.get((item['source'], item['session']))):
                item['resume_hold'] = hold
            if row['archived_at'] is not None:
                item['archived_at'] = row['archived_at']
            item['fresh'] = time.time() - item['updated_at'] <= item['lease_seconds']
            # A host liveness watcher proves the process ended: say so instead of guessing.
            # Any event newer than the exit report means the session came back; a reading
            # older than a day is board furniture, not a live fact.
            item['exited'] = (item.get('liveness') == 'exited' and not item['fresh']
                              and item['updated_at'] <= item.get('liveness_at', item['updated_at'])
                              and time.time() - item.get('liveness_at', 0) <= 24 * 3600)
            if item['exited']:
                item['effective_state'] = 'exited'
            else:
                item['effective_state'] = item['state'] if item['fresh'] or item['state'] in TERMINAL else 'lost_contact'
            # A prompt raised by an auto-approving agent resolves itself within seconds; only
            # paint Waiting once the beat lapses. Say what is actually being waited on.
            if item.get('approval_pending') and item['effective_state'] == 'waiting':
                gate = item.get('approval_gate', 'human')
                if gate == 'auto' and time.time() - item['updated_at'] <= APPROVAL_BEAT:
                    item['effective_state'] = 'working'
                    item['blocked_on'] = 'automation'
                else:
                    item['blocked_on'] = 'human' if gate == 'human' else 'automation'
            if filters.get('project') and filters['project'] not in (item.get('project'),item.get('project_key'),item.get('project_id'),label):
                continue
            if any(item.get(k if k != 'state' else 'effective_state') != v for k, v in filters.items() if k in ('source', 'session', 'agent', 'machine', 'state')):
                continue
            if filters.get('q', '').lower() not in json.dumps(item).lower():
                continue
            result.append(item)
        return result

    def ingest_resource(self, snapshot):
        value = validate_resource(snapshot)
        with self.connect() as db:
            changed = db.execute('INSERT INTO resources VALUES(?,?,?,?) ON CONFLICT(provider,account) DO UPDATE SET observed_at=excluded.observed_at,payload=excluded.payload WHERE excluded.observed_at>resources.observed_at',
                       (value['provider'],value['account'],value['observed_at'],json.dumps(value))).rowcount
        if changed:
            self.changed()
        return bool(changed)

    def resources(self):
        with self.connect() as db:
            rows = [json.loads(row[0]) for row in db.execute('SELECT payload FROM resources ORDER BY provider,account')]
        for provider in ('codex','claude'):
            if not any(row['provider']==provider for row in rows):
                rows.append({'provider':provider,'account':'primary','observed_at':None,'windows':[]})
        return rows

    def token_usage(self, since, session_keys=None):
        """Tokens each agent sent and received since a time, from per-session running totals.
        A session already under way before the period with no earlier count is measured from its
        first count inside the period, so turning reporting on never dumps a whole session into it."""
        with self.connect() as db:
            counted = db.execute("""SELECT DISTINCT source, session FROM events
                WHERE at>=? AND payload LIKE '%"tokens"%'""", (since,)).fetchall()
            rows = []
            for source, session in counted:
                if session_keys is not None and (source,session) not in session_keys:
                    continue
                agent = db.execute("SELECT json_extract(payload,'$.agent') FROM sessions WHERE source=? AND session=?", (source, session)).fetchone()
                began = db.execute('SELECT MIN(at) FROM events WHERE source=? AND session=?', (source, session)).fetchone()[0]
                for at, tokens in db.execute("""SELECT at, json_extract(payload,'$.data.tokens') FROM events
                        WHERE source=? AND session=? AND payload LIKE '%"tokens"%' ORDER BY at""", (source, session)):
                    rows.append({'source': source, 'session': session, 'at': at, 'tokens': tokens,
                                 'agent': agent and agent[0], 'began': began})
        sessions = {}
        for row in rows:
            tokens = json.loads(row['tokens']) if row['tokens'] else None
            if not isinstance(tokens, dict):
                continue
            entry = sessions.setdefault((row['source'], row['session']), {'agent': row['agent'], 'base': None, 'last': None})
            if row['at'] < since:
                entry['base'] = tokens
            else:
                if entry['base'] is None and entry['last'] is None:
                    entry['base'] = tokens if row['began'] < since else {}
                entry['last'] = tokens
        usage = {}
        for entry in sessions.values():
            if not entry['last'] or not entry['agent']:
                continue
            total = usage.setdefault(entry['agent'], {'input': 0, 'cached': 0, 'output': 0, 'sessions': 0})
            # A count that went backwards is a restarted transcript; what it shows now is all new.
            for key in ('input', 'cached', 'output'):
                change = entry['last'][key] - entry['base'].get(key, 0)
                total[key] += change if change >= 0 else entry['last'][key]
            total['sessions'] += 1
        return usage

    def activity_times(self):
        """Heartbeat freshness must not reshuffle work; heartbeat-only sessions use first sighting."""
        with self.connect() as db:
            rows = db.execute("SELECT source, session, COALESCE(MAX(CASE WHEN kind!='heartbeat' THEN at END), MIN(at)) AS activity_at FROM events GROUP BY source, session").fetchall()
        return {(row['source'], row['session']): row['activity_at'] for row in rows}

    def archive(self, source, session, archived):
        if type(archived) is not bool:
            raise ValueError('archived must be a boolean')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM sessions WHERE source=? AND session=?', (source, session)).fetchone():
                raise LookupError('unknown session')
            if archived:
                db.execute('INSERT OR IGNORE INTO session_archives VALUES(?,?,?)', (source, session, time.time()))
            else:
                db.execute('DELETE FROM session_archives WHERE source=? AND session=?', (source, session))
        self.changed()
        return {'archived': archived}

    def events(self, filters=None):
        filters = filters or {}
        clauses, args = [], []
        for key in ('source', 'session'):
            if filters.get(key):
                clauses.append(key + '=?'); args.append(filters[key])
        if filters.get('kind'):
            clauses.append('kind=?'); args.append(filters['kind'])
        for key, op in [('since', '>='), ('until', '<')]:
            if filters.get(key):
                clauses.append('at' + op + '?'); args.append(float(filters[key]))
        if filters.get('project'):
            matches = self.sessions({'project':filters['project']})
            clauses.append('(source,session) IN (VALUES '+','.join('(?,?)' for _ in matches)+')' if matches else '0')
            for item in matches:
                args.extend((item['source'],item['session']))
        for field in ('agent', 'machine'):
            if filters.get(field):
                clauses.append("EXISTS (SELECT 1 FROM sessions s WHERE s.source=events.source AND s.session=events.session AND json_extract(s.payload, '$." + field + "')=?)")
                args.append(filters[field])
        if filters.get('q'):
            clauses.append('instr(lower(payload), lower(?))>0'); args.append(filters['q'])
        if filters.get('meaningful') == '1':
            clauses.append("(json_extract(payload,'$.data.step') <> '' OR "
                           "json_extract(payload,'$.data.goal') <> '' OR "
                           "json_extract(payload,'$.data.outcome') <> '' OR "
                           "json_extract(payload,'$.data.blocker') <> '')")
        if filters.get('exclude_kind'):
            clauses.append('kind<>?'); args.append(filters['exclude_kind'])
        order = filters.get('order', 'asc')
        if order not in ('asc', 'desc'):
            raise ValueError('invalid event order')
        if filters.get('before'):
            clauses.append('seq<?'); args.append(max(0, int(filters['before'])))
        after = max(0, int(filters.get('after', 0)))
        limit = min(500, max(1, int(filters.get('limit', 100))))
        clauses.append('seq>?'); args.append(after)
        with self.connect() as db:
            rows = db.execute('SELECT * FROM events WHERE ' + ' AND '.join(clauses) + ' ORDER BY seq ' + order.upper() + ' LIMIT ?', args + [limit]).fetchall()
        return [{'seq': r['seq'], 'received_at': r['received'], **json.loads(r['payload'])} for r in rows]


    def send_message(self, source, session, text):
        """Queue dashboard input for one existing session; its hooks claim it."""
        if not (IDENTITY.fullmatch(source) and IDENTITY.fullmatch(session)):
            raise ValueError('invalid session identity')
        if not isinstance(text, str) or not text.strip() or len(text) > MESSAGE_LIMIT:
            raise ValueError('message must be 1..%d characters' % MESSAGE_LIMIT)
        message = {'id': uuid.uuid4().hex, 'source': source, 'session': session, 'at': time.time(),
                   'text': text.strip(), 'delivered_at': None}
        with self.connect() as db:
            if not db.execute('SELECT 1 FROM sessions WHERE source=? AND session=?', (source, session)).fetchone():
                raise LookupError('unknown session')
            db.execute('INSERT INTO messages(id,source,session,at,text,delivered_at) VALUES(:id,:source,:session,:at,:text,:delivered_at)', message)
            # A new message is the user asking again: a refused resume gets another try.
            db.execute('DELETE FROM resume_holds WHERE source=? AND session=?', (source, session))
        with self.inbox:
            self.inbox.notify_all()
        self.changed()
        return message

    def queued(self):
        """Undelivered input per session, for the board's chat buttons."""
        with self.connect() as db:
            rows = db.execute('SELECT source, session, COUNT(*) FROM messages WHERE delivered_at IS NULL GROUP BY source, session').fetchall()
        return {(r[0], r[1]): r[2] for r in rows}

    def messages(self, source, session):
        with self.connect() as db:
            rows = db.execute('SELECT m.id,m.source,m.session,m.at,m.text,m.delivered_at,w.warning AS wake_warning FROM messages m LEFT JOIN message_wake_warnings w ON w.message_id=m.id WHERE m.source=? AND m.session=? ORDER BY m.at', (source, session)).fetchall()
        return [{k:v for k,v in dict(r).items() if k != 'wake_warning' or v is not None} for r in rows]

    def mint_host_credential(self, machine):
        if not isinstance(machine, str) or not MACHINE.fullmatch(machine):
            raise ValueError('invalid machine')
        credential = secrets.token_urlsafe(32)
        digest = hashlib.sha256(credential.encode()).hexdigest()
        with self.connect() as db:
            db.execute('INSERT INTO host_credentials VALUES(?,?,?,NULL)', (digest, machine, time.time()))
        return {'machine': machine, 'credential': credential, 'credential_id': digest}

    def revoke_host_credential(self, digest):
        with self.connect() as db:
            db.execute('UPDATE host_credentials SET revoked_at=? WHERE digest=?', (time.time(), digest))
        with self.inbox:
            self.inbox.notify_all()

    def claim_machine(self, db, credential, source, session):
        machine = self.host_machine(credential, db)
        row = db.execute('SELECT payload FROM sessions WHERE source=? AND session=?', (source, session)).fetchone()
        if not row or json.loads(row['payload']).get('machine') != machine:
            raise PermissionError('invalid host credential or session ownership')
        return machine

    def host_machine(self, credential, db):
        digest = hashlib.sha256(credential.encode()).hexdigest()
        host = db.execute('SELECT machine FROM host_credentials WHERE digest=? AND revoked_at IS NULL', (digest,)).fetchone()
        if not host:
            raise PermissionError('invalid host credential')
        return host['machine']

    def record_sync(self, credential, payload):
        if not isinstance(payload, dict) or set(payload) != {'synced_revision', 'ok', 'error'}:
            raise ValueError('invalid sync status')
        revision, ok, error = payload['synced_revision'], payload['ok'], payload['error']
        if not isinstance(revision, str) or not re.fullmatch(r'[a-f0-9]{40,64}|unknown', revision):
            raise ValueError('invalid revision')
        if type(ok) is not bool or error not in (None, 'server_unavailable', 'sync_failed') or (ok and error is not None):
            raise ValueError('invalid sync result')
        with self.connect() as db:
            machine = self.host_machine(credential, db)
            db.execute('INSERT OR REPLACE INTO host_sync VALUES(?,?,?,?,?)', (machine,time.time(),revision,ok,error))
        self.changed()
        return {'recorded': True}

    def hosts(self, revision=None):
        with self.connect() as db:
            sync = {r['machine']: dict(r) for r in db.execute('SELECT * FROM host_sync')}
            runners = {r['machine']: {'seen_at': r['seen_at'], 'resume_agents': json.loads(r['resume_agents']),
                                      'starts': json.loads(r['starts'])}
                       for r in db.execute('SELECT * FROM host_runners')}
        machines = {s.get('machine') for s in self.sessions() if s.get('machine')} | set(sync) | set(runners)
        return [{'machine': machine, **sync.get(machine, {}),
                 **({'runner': runners[machine]} if machine in runners else {}),
                 'behind': bool(revision and sync.get(machine, {}).get('synced_revision') != revision)}
                for machine in sorted(machines)]

    def enqueue_job(self, machine, payload):
        """Queue a start job for one host's runner (#19).

        Projects travel as bare names: the server never sees or stores folder
        paths, the runner resolves names host-local and refuses unknown ones."""
        if not isinstance(machine, str) or not MACHINE.fullmatch(machine):
            raise ValueError('invalid machine')
        if not isinstance(payload, dict):
            raise ValueError('invalid job')
        agent, project, prompt = payload.get('agent'), payload.get('project'), payload.get('prompt')
        if agent not in JOB_AGENTS:
            raise ValueError('unknown agent')
        if not bare_project(project):
            raise ValueError('project must be a bare name of at most 80 characters')
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MESSAGE_LIMIT:
            raise ValueError('prompt must be 1..%d characters' % MESSAGE_LIMIT)
        worktree = payload.get('worktree', True)
        if type(worktree) is not bool:
            raise ValueError('worktree must be a boolean')
        job = {'id': uuid.uuid4().hex, 'machine': machine, 'at': time.time(), 'agent': agent,
               'project': project.strip(), 'prompt': prompt.strip(), 'worktree': worktree,
               'status': 'queued', 'detail': None, 'claimed_at': None, 'ended_at': None}
        with self.connect() as db:
            db.execute('INSERT INTO host_jobs VALUES(:id,:machine,:at,:agent,:project,:prompt,'
                       ':worktree,:status,:detail,:claimed_at,:ended_at)',
                       {**job, 'worktree': int(worktree)})
        self.changed()
        with self.inbox:
            self.inbox.notify_all()
        return job

    def claim_job(self, machine, credential, wait=0, starts=None):
        """Atomically hand a host its oldest queued job; it stays claimed until reported.

        A runner names what it may start (project names and agents, never paths) with
        each claim, so the board offers only what the host would accept."""
        if not isinstance(machine, str) or not MACHINE.fullmatch(machine):
            raise ValueError('invalid machine')
        if starts is not None:
            starts = start_offers(starts)
            with self.connect() as db:
                if self.host_machine(credential, db) != machine:
                    raise PermissionError('invalid host credential or machine')
                db.execute("INSERT INTO host_runners VALUES(?,?,'[]',?) ON CONFLICT(machine) DO UPDATE"
                           ' SET seen_at=excluded.seen_at, starts=excluded.starts', (machine, time.time(), json.dumps(starts)))
            self.changed()
        deadline = time.time() + min(CLAIM_WAIT_LIMIT, max(0, wait))
        while True:
            with self.connect() as db:
                if self.host_machine(credential, db) != machine:
                    raise PermissionError('invalid host credential or machine')
                row = db.execute("SELECT * FROM host_jobs WHERE machine=? AND status='queued'"
                                 ' ORDER BY at,id LIMIT 1', (machine,)).fetchone()
                if row and db.execute("UPDATE host_jobs SET status='claimed', claimed_at=?"
                                      " WHERE id=? AND status='queued'", (now := time.time(), row['id'])).rowcount:
                    job = dict(row)
                    job.update(status='claimed', claimed_at=now, worktree=bool(row['worktree']))
                    return job
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            with self.inbox:
                self.inbox.wait(min(remaining, 1))

    def report_job(self, machine, credential, job_id, payload):
        """Runner-side job lifecycle reports; transitions follow JOB_STATES, terminal states stick."""
        if not isinstance(payload, dict):
            raise ValueError('invalid report')
        status, detail = payload.get('status'), payload.get('detail')
        if status not in JOB_REPORTS:
            raise ValueError('invalid job status')
        if detail is not None and (not isinstance(detail, str) or not 0 < len(detail) <= JOB_DETAIL_LIMIT):
            raise ValueError('detail must be at most %d characters' % JOB_DETAIL_LIMIT)
        with self.connect() as db:
            if self.host_machine(credential, db) != machine:
                raise PermissionError('invalid host credential or machine')
            row = db.execute('SELECT status FROM host_jobs WHERE id=? AND machine=?', (job_id, machine)).fetchone()
            if row is None:
                raise LookupError('unknown job')
            if status not in JOB_STATES.get(row['status'], ()):
                raise ValueError('invalid job transition from %s' % row['status'])
            db.execute('UPDATE host_jobs SET status=?, detail=COALESCE(?, detail), ended_at=? WHERE id=?',
                       (status, detail, None if status == 'started' else time.time(), job_id))
        self.changed()
        return {'id': job_id, 'status': status}

    def jobs(self, machine=None):
        """Recent jobs newest first; the board reads these, prompts are user-authored input."""
        with self.connect() as db:
            rows = db.execute('SELECT * FROM host_jobs ' + ('WHERE machine=? ' if machine else '') +
                              'ORDER BY at DESC LIMIT ?', ((machine,) if machine else ()) + (JOB_LIST_LIMIT,)).fetchall()
        return [{**dict(r), 'worktree': bool(r['worktree'])} for r in rows]

    def claim_resume(self, machine, credential, agents, wait=0):
        """Hand a host runner the pending input of one dormant session on that host.

        Dormant means wake consent is on, the session is not archived, and nothing
        in it can take the input: no armed listener or queue watcher, and not mid-turn.
        The runner still checks the agent's own liveness before resuming."""
        if not isinstance(machine, str) or not MACHINE.fullmatch(machine):
            raise ValueError('invalid machine')
        agents = sorted({a for a in agents if a in RESUME_PATHS})
        deadline = time.time() + min(CLAIM_WAIT_LIMIT, max(0, wait))
        while True:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                now = time.time()
                if self.host_machine(credential, db) != machine:
                    raise PermissionError('invalid host credential or machine')
                db.execute("INSERT INTO host_runners VALUES(?,?,?,'[]') ON CONFLICT(machine) DO UPDATE"
                           ' SET seen_at=excluded.seen_at, resume_agents=excluded.resume_agents',
                           (machine, now, json.dumps(agents)))
                found = None
                candidates = db.execute(
                    'SELECT m.source, m.session, s.payload FROM messages m'
                    ' JOIN sessions s ON s.source=m.source AND s.session=m.session'
                    ' JOIN session_wake w ON w.source=m.source AND w.session=m.session AND w.enabled'
                    ' LEFT JOIN message_claims c ON c.message_id=m.id'
                    ' LEFT JOIN session_archives a ON a.source=m.source AND a.session=m.session'
                    ' LEFT JOIN resume_holds h ON h.source=m.source AND h.session=m.session AND h.until_at>?'
                    ' WHERE m.delivered_at IS NULL AND (c.until_at IS NULL OR c.until_at<=?) AND m.at<=?'
                    ' AND a.session IS NULL AND h.session IS NULL'
                    ' GROUP BY m.source, m.session ORDER BY MIN(m.at)', (now, now, now - RESUME_GRACE)).fetchall()
                for row in candidates:
                    payload = json.loads(row['payload'])
                    if payload.get('machine') == machine and payload.get('agent') in agents and dormant(payload, now):
                        found = (row['source'], row['session'], payload['agent'])
                        break
                if found:
                    source, session, agent = found
                    lease = uuid.uuid4().hex
                    rows = [dict(r) for r in db.execute(
                        'SELECT m.* FROM messages m LEFT JOIN message_claims c ON c.message_id=m.id'
                        ' WHERE source=? AND session=? AND delivered_at IS NULL AND (c.until_at IS NULL OR c.until_at<=?)'
                        ' ORDER BY at LIMIT 100', (source, session, now)).fetchall()]
                    for row in rows:
                        row['claim_token'] = lease
                        db.execute('INSERT OR REPLACE INTO message_claims VALUES(?,?,?,?)',
                                   (row['id'], lease, now + WAKE_LEASE, RESUME_PATHS[agent]))
            if found:
                self.changed()
                return {'source': source, 'session': session, 'agent': agent, 'messages': rows}
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            with self.inbox:
                self.inbox.wait(min(remaining, 1))

    def set_wake(self, source, session, enabled):
        if type(enabled) is not bool:
            raise ValueError('wake must be a boolean')
        with self.connect() as db:
            if not db.execute('SELECT 1 FROM sessions WHERE source=? AND session=?', (source, session)).fetchone():
                raise LookupError('unknown session')
            db.execute('INSERT OR REPLACE INTO session_wake VALUES(?,?,?)', (source, session, enabled))
            db.execute('DELETE FROM resume_holds WHERE source=? AND session=?', (source, session))
        self.changed()
        with self.inbox:
            self.inbox.notify_all()
        return {'wake': enabled}

    def claim(self, source, session, wait=0, credential=None, wake_path=None, cancelled=None):
        """Atomically hand pending messages to the session's hook, waiting up to `wait` seconds."""
        if not (IDENTITY.fullmatch(source) and IDENTITY.fullmatch(session)):
            raise ValueError('invalid session identity')
        deadline = time.time() + min(CLAIM_WAIT_LIMIT, max(0, wait))
        if wake_path is not None and (wake_path not in WAKE_PATHS or not credential):
            raise PermissionError('authenticated wake path required')
        while True:
            if cancelled and cancelled():
                return []
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                now = time.time()
                blocked = False
                if credential is not None:
                    self.claim_machine(db, credential, source, session)
                if wake_path:
                    consent = db.execute('SELECT enabled FROM session_wake WHERE source=? AND session=?',(source,session)).fetchone()
                    owner = db.execute('SELECT payload FROM sessions WHERE source=? AND session=?',(source,session)).fetchone()
                    archived = db.execute('SELECT 1 FROM session_archives WHERE source=? AND session=?',(source,session)).fetchone()
                    blocked = bool(archived or not consent or not consent[0] or json.loads(owner[0]).get('agent') != WAKE_PATHS[wake_path])
                if blocked:
                    # Consent off must not answer instantly: a waiting listener would
                    # re-claim in a tight loop all night. Hold the wait; set_wake
                    # notifies this condition, so delivery resumes the moment consent flips.
                    rows = []
                else:
                    rows = [dict(r) for r in db.execute('SELECT m.* FROM messages m LEFT JOIN message_claims c ON c.message_id=m.id WHERE source=? AND session=? AND delivered_at IS NULL AND (c.until_at IS NULL OR c.until_at<=?) ORDER BY at LIMIT 100',
                                                        (source, session, now)).fetchall()]
                lease = uuid.uuid4().hex
                for row in rows:
                    if wake_path:
                        row['claim_token'] = lease
                        db.execute('INSERT OR REPLACE INTO message_claims VALUES(?,?,?,?)',
                                   (row['id'],lease,now+WAKE_LEASE,wake_path))
                    else:
                        row['delivered_at'] = now
                        db.execute('UPDATE messages SET delivered_at=? WHERE id=?', (now, row['id']))
                        db.execute('DELETE FROM message_claims WHERE message_id=?',(row['id'],))
            if rows:
                self.changed()  # queued counts on the board drop
            remaining = deadline - time.time()
            if rows or remaining <= 0:
                return rows
            with self.inbox:
                # Recheck ownership and revocation even if a notification raced this wait.
                self.inbox.wait(min(remaining, 1))

    def acknowledge_wake(self, source, session, credential, payload):
        if not isinstance(payload,dict) or not {'ids','claim_token','accepted'} <= set(payload) <= {'ids','claim_token','accepted','error','warning'}:
            raise ValueError('invalid acknowledgment')
        ids, lease, accepted, error = payload['ids'], payload['claim_token'], payload['accepted'], payload.get('error')
        if error is not None and (accepted is not False or not isinstance(error,str) or not RESUME_REASON.fullmatch(error)):
            raise ValueError('invalid refusal reason')
        if not isinstance(ids,list) or not 1<=len(ids)<=100 or any(not isinstance(i,str) or not IDENTITY.fullmatch(i) for i in ids):
            raise ValueError('invalid message IDs')
        if not isinstance(lease,str) or not re.fullmatch('[a-f0-9]{32}',lease) or type(accepted) is not bool:
            raise ValueError('invalid acknowledgment')
        warning=payload.get('warning')
        if warning is not None and (accepted is not True or warning != 'codex_queue_wake_failed'):
            raise ValueError('invalid wake warning')
        events=[]
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.claim_machine(db,credential,source,session)
            for message_id in set(ids):
                row=db.execute('SELECT m.*,c.token AS claim_token,c.until_at AS claim_until,c.path AS claim_path FROM messages m JOIN message_claims c ON c.message_id=m.id WHERE source=? AND session=? AND id=? AND c.token=?',
                               (source,session,message_id,lease)).fetchone()
                if not row:
                    raise ValueError('unknown claim')
                if row['delivered_at'] is not None:
                    continue  # A retried acknowledgment is idempotent.
                if not row['claim_until'] or row['claim_until']<time.time():
                    raise ValueError('expired claim')
                if accepted:
                    if warning:
                        db.execute('INSERT OR REPLACE INTO message_wake_warnings VALUES(?,?)',(message_id,warning))
                    db.execute('UPDATE messages SET delivered_at=? WHERE id=?',(time.time(),message_id))
                    events.append(dict(version=1,source=source,session=session,id='wake-'+hashlib.sha256((message_id+lease).encode()).hexdigest()[:32],
                        at=time.time(),kind='heartbeat',data={'activity':'Dashboard input token via '+row['claim_path']}))
                else:
                    db.execute('DELETE FROM message_claims WHERE message_id=?',(message_id,))
                    if error and row['claim_path'] in RESUME_PATHS.values():
                        # Say why on the board, and keep the runner from re-claiming at once.
                        db.execute('INSERT OR REPLACE INTO resume_holds VALUES(?,?,?,?)',
                                   (source, session, time.time() + RESUME_HOLD, error))
            # Insert audit events in the same transaction as delivery so a restart cannot lose them.
            for event in events:
                db.execute('INSERT OR IGNORE INTO events(source,session,id,at,received,kind,payload) VALUES(?,?,?,?,?,?,?)',
                    (source,session,event['id'],event['at'],event['at'],event['kind'],json.dumps(event)))
        self.changed()
        with self.inbox:
            self.inbox.notify_all()
        return {'acknowledged':len(ids)}


def dormant(payload, now):
    """True when nothing in the session can take input itself (#18).

    A proven exit (SessionEnd, or the liveness watcher) wins over a listener
    deadline: the listener died with the process but its deadline stays reported."""
    if payload.get('state') == 'stopped' or (
            payload.get('liveness') == 'exited' and payload.get('updated_at', 0) <= payload.get('liveness_at', 0)):
        return True
    if (payload.get('listening_until') or 0) > now:
        return False
    fresh = now - payload.get('updated_at', 0) <= payload.get('lease_seconds', 900)
    return not (fresh and payload.get('state') == 'working')


def update_status(root):
    """What the deploy updater last did, without the host paths it records."""
    try:
        state = json.loads((root/'update-state.json').read_text())
    except (OSError, ValueError):
        state = {}
    try:
        requested = (root/'update-requested').stat().st_mtime
    except OSError:
        requested = None
    return {'commit': state.get('commit'), 'release': state.get('release'), 'previous': state.get('previous'),
            'deployed_at': state.get('deployed_at'), 'failed': state.get('failed'), 'requested_at': requested}


def request_update(root):
    """Ask the updater for a run that waits for CI; repeated requests while one is fresh start nothing new."""
    request = root/'update-requested'
    try:
        fresh = time.time()-request.stat().st_mtime < UPDATE_DEBOUNCE
    except OSError:
        fresh = False
    request.touch()
    if not fresh:
        # --no-block: the run may restart this very server, so the request must not wait for it.
        subprocess.run(['systemctl', '--user', 'start', '--no-block', UPDATE_UNIT], check=True, timeout=10, capture_output=True)
    return update_status(root)


def handler(journal, web=True, token=None, stop=None, updater=None, prometheus_url=None, inference=(), allow_legacy_claims=False):
    revision = server_revision()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            filters = {k: v[-1] for k, v in parse_qs(url.query).items()}
            if url.path == '/stream':
                self.stream(); return
            try:
                if web and url.path in WEB_ASSETS:
                    asset, content_type = WEB_ASSETS[url.path]
                    body = files('axon_server').joinpath('web', *asset.split('/')).read_bytes()
                    if asset == 'index.html':
                        body = body.replace(b'<html lang="en"', ('<html lang="en" data-axon-version="'+revision+'"').encode(), 1)
                    self.send_response(200)
                    self.send_header('Content-Type', content_type)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers(); self.wfile.write(body); return
                if url.path == '/health':
                    data = {'ok': True, 'retention_days': journal.retention_days, 'protocol': capabilities(),
                            'host_claims': {'enrollment': True, 'required': not allow_legacy_claims, 'wake_leases':True},
                            'host_jobs': True, 'host_resume': sorted(RESUME_PATHS)}
                elif url.path == '/resources':
                    data = {'resources': journal.resources()}
                elif url.path == '/resources/tokens':
                    data = {'tokens': journal.token_usage(float(filters.get('since', '')))}
                elif url.path == '/resources/history':
                    if not prometheus_url or filters.get('account') not in inference:
                        self.send_error(404); return
                    span = int(filters.get('range', 3600))
                    try:
                        data = inference_history(prometheus_url, filters['account'], span)
                    except (OSError, ValueError, LookupError, TypeError):
                        self.send_error(502, 'Prometheus unavailable'); return
                elif url.path == '/update' and updater:
                    data = update_status(updater)
                elif url.path == '/sessions':
                    queued = journal.queued()
                    activity = journal.activity_times()
                    data = {'sessions': [{**s, 'activity_at': activity.get((s['source'], s['session']), s['updated_at']), 'queued_messages': queued.get((s['source'], s['session']), 0)}
                                         for s in journal.sessions(filters)]}
                elif url.path == '/hosts':
                    data = {'hosts': journal.hosts(update_status(updater).get('commit') if updater else None)}
                elif (job_route := host_job_path(url.path)) and len(job_route) == 4:
                    data = {'jobs': journal.jobs(job_route[2])}
                elif url.path == '/projects':
                    data = {'projects': journal.projects(float(filters['since']) if 'since' in filters else None)}
                elif url.path.startswith('/projects/'):
                    key = unquote(url.path[len('/projects/'):])
                    data = next((p for p in journal.projects(float(filters['since']) if 'since' in filters else None) if p['id']==key), None)
                    if data is None:
                        self.send_error(404); return
                elif (parts := session_path(url.path)) and parts[2] == 'messages':
                    data = {'messages': journal.messages(parts[0], parts[1]), 'input': bool(token)}
                elif url.path == '/events':
                    events = journal.events(filters)
                    data = {'events': events, 'next_after': max(e['seq'] for e in events) if events else int(filters.get('after', 0))}
                    if filters.get('order') == 'desc':
                        data['next_before'] = events[-1]['seq'] if events else None
                else:
                    self.send_error(404); return
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers(); self.wfile.write(body)
            except (ValueError, TypeError):
                self.send_error(400, 'Invalid query')
        def authorized(self, disabled):
            # Input and deploys make things act, so they always need the configured token.
            if not token:
                self.send_error(403, disabled); return False
            if not hmac.compare_digest(self.headers.get('Authorization', '').encode(), ('Bearer ' + token).encode()):
                self.send_error(401, 'Invalid input token'); return False
            return True
        def do_POST(self):
            url = urlsplit(self.path)
            if url.path.startswith('/projects/'):
                if not self.authorized('Project overrides are disabled'): return
                try:
                    length = int(self.headers.get('Content-Length', 0))
                    if not 0 < length <= 2048: raise ValueError('invalid body')
                    data = journal.update_project(unquote(url.path[len('/projects/'):]),json.loads(self.rfile.read(length)))
                except LookupError:
                    self.send_error(404, 'Unknown project'); return
                except (ValueError, TypeError):
                    self.send_error(400, 'Invalid override'); return
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers(); self.wfile.write(body); return
            if url.path == '/hosts/sync':
                auth = self.headers.get('Authorization', '')
                try:
                    length = int(self.headers.get('Content-Length', 0))
                    if not 0 < length <= 2048: raise ValueError('invalid body')
                    credential = auth[7:] if auth.startswith('Bearer ') else ''
                    data = journal.record_sync(credential, json.loads(self.rfile.read(length)))
                except PermissionError:
                    self.send_error(401, 'Invalid host credential'); return
                except (ValueError, TypeError):
                    self.send_error(400, 'Invalid sync status'); return
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers(); self.wfile.write(body); return
            if url.path in ('/hosts/credentials', '/hosts/credentials/revoke'):
                if not self.authorized('Host enrollment is disabled'): return
                try:
                    length = int(self.headers.get('Content-Length', 0))
                    if not 0 < length <= 1024: raise ValueError('invalid body')
                    payload = json.loads(self.rfile.read(length))
                    if url.path.endswith('/revoke'):
                        journal.revoke_host_credential(payload['credential_id'])
                        data = {'revoked': True}
                    else:
                        data = journal.mint_host_credential(payload['machine'])
                    body = json.dumps(data).encode()
                except (ValueError, KeyError, TypeError):
                    self.send_error(400, 'Invalid enrollment'); return
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers(); self.wfile.write(body); return
            if (resume_route := host_resume_path(url.path)):
                auth = self.headers.get('Authorization', '')
                credential = auth[7:] if auth.startswith('Bearer ') else ''
                if not credential:
                    self.send_error(401, 'Host credential required'); return
                try:
                    params = parse_qs(url.query)
                    agents = [a for a in params.get('agents', [''])[-1].split(',') if a]
                    data = {'resume': journal.claim_resume(resume_route[2], credential, agents,
                                                           float(params.get('wait', ['0'])[-1]))}
                except PermissionError:
                    self.send_error(401, 'Invalid host credential'); return
                except (ValueError, TypeError):
                    self.send_error(400, 'Invalid resume claim'); return
                if self.disconnected(): return
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers(); self.wfile.write(body); return
            if (job_route := host_job_path(url.path)):
                machine = job_route[2]
                action = job_route[4] if len(job_route) == 5 else job_route[5] if len(job_route) == 6 else ''
                if len(job_route) == 4:
                    if not self.authorized('Host jobs are disabled'): return
                    try:
                        length = int(self.headers.get('Content-Length', 0))
                        if not 0 < length <= 4 * MESSAGE_LIMIT + 1024: raise ValueError('invalid body')
                        data, status = {'job': journal.enqueue_job(machine, json.loads(self.rfile.read(length)))}, 201
                    except (ValueError, TypeError):
                        self.send_error(400, 'Invalid job'); return
                else:
                    auth = self.headers.get('Authorization', '')
                    credential = auth[7:] if auth.startswith('Bearer ') else ''
                    if not credential:
                        self.send_error(401, 'Host credential required'); return
                    try:
                        if action == 'claim':
                            wait = float(parse_qs(url.query).get('wait', ['0'])[-1])
                            length = int(self.headers.get('Content-Length', 0))
                            if not 0 <= length <= 64 * 1024: raise ValueError('invalid body')
                            # Runners from before start offers send no body; leave their offers unknown.
                            body = json.loads(self.rfile.read(length)) if length else {}
                            if not isinstance(body, dict): raise ValueError('invalid body')
                            starts = body.get('starts')
                            data, status = {'job': journal.claim_job(machine, credential, wait, starts)}, 200
                        elif action == 'report':
                            length = int(self.headers.get('Content-Length', 0))
                            if not 0 < length <= JOB_DETAIL_LIMIT + 1024: raise ValueError('invalid body')
                            data, status = journal.report_job(machine, credential, job_route[4], json.loads(self.rfile.read(length))), 200
                        else:
                            self.send_error(404); return
                    except PermissionError:
                        self.send_error(401, 'Invalid host credential'); return
                    except LookupError:
                        self.send_error(404, 'Unknown job'); return
                    except (ValueError, TypeError):
                        self.send_error(400, 'Invalid job request'); return
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers(); self.wfile.write(body); return
            if url.path == '/update' and updater:
                if not self.authorized('Update requests are disabled'): return
                try:
                    body = json.dumps(request_update(updater)).encode()
                except (OSError, subprocess.SubprocessError):
                    logging.exception('Could not start the updater')
                    self.send_error(503, 'Updater unavailable'); return
                self.send_response(202)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers(); self.wfile.write(body); return
            parts = session_path(url.path)
            if not parts or parts[2] not in ('messages', 'inbox/claim', 'inbox/wake/claim', 'inbox/wake/ack', 'archive', 'wake'):
                self.send_error(501); return
            try:
                if parts[2] in ('messages', 'archive', 'wake'):
                    if not self.authorized('Session input is disabled'): return
                    length = int(self.headers.get('Content-Length', 0))
                    if not 0 < length <= 4 * MESSAGE_LIMIT + 1024:
                        raise ValueError('invalid body')
                    payload = json.loads(self.rfile.read(length))
                    if parts[2] == 'wake':
                        data, status = journal.set_wake(parts[0], parts[1], payload.get('wake')), 200
                    elif parts[2] == 'archive':
                        data, status = journal.archive(parts[0], parts[1], payload.get('archived')), 200
                    else:
                        data, status = journal.send_message(parts[0], parts[1], payload.get('text')), 201
                else:
                    auth = self.headers.get('Authorization', '')
                    credential = auth[7:] if auth.startswith('Bearer ') else None
                    if not credential:
                        if not allow_legacy_claims or parts[2]!='inbox/claim':
                            self.send_error(401, 'Host credential required'); return
                        logging.warning('Legacy unauthenticated inbox claim')
                    if parts[2].startswith('inbox/wake/') and allow_legacy_claims:
                        self.send_error(403, 'Unattended wake requires claim enforcement'); return
                    if parts[2]=='inbox/wake/ack':
                        length=int(self.headers.get('Content-Length',0))
                        if not 0<length<=16384:raise ValueError('invalid body')
                        data,status=journal.acknowledge_wake(parts[0],parts[1],credential,json.loads(self.rfile.read(length))),200
                    else:
                        params=parse_qs(url.query)
                        wait = float(params.get('wait', ['0'])[-1])
                        if parts[2]=='inbox/claim' and wait>0:
                            raise ValueError('Idle delivery requires a leased wake claim')
                        wake_path=params.get('path',[''])[-1] if parts[2]=='inbox/wake/claim' else None
                        data, status = {'messages': journal.claim(parts[0], parts[1], wait, credential=credential,
                            wake_path=wake_path,cancelled=self.disconnected)}, 200
                if self.disconnected():return
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers(); self.wfile.write(body)
            except PermissionError:
                self.send_error(401, 'Invalid host credential')
            except LookupError:
                self.send_error(404, 'Unknown session')
            except (ValueError, TypeError, AttributeError):
                self.send_error(400, 'Invalid request')

        def disconnected(self):
            if stop and stop.is_set():return True
            try:
                readable,_,_=select.select([self.connection],[],[],0)
                return bool(readable and self.connection.recv(1,socket.MSG_PEEK)==b'')
            except OSError:
                return True

        def stream(self):
            """Server-sent change signals; the page refetches through the normal API."""
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('X-Accel-Buffering', 'no')
            self.end_headers()
            seen = journal.version
            try:
                # The first frame makes a reconnecting page catch up on anything it missed.
                self.wfile.write(f'retry: 3000\ndata: {seen}\n\n'.encode())
                while not (stop and stop.is_set()):
                    version = journal.wait_change(seen, STREAM_PING)
                    self.wfile.write((f'data: {version}\n\n' if version != seen else ': ping\n\n').encode())
                    seen = version
            except OSError:
                pass  # the page went away

        def end_headers(self):
            self.send_header('X-Axon-Version', revision)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            super().end_headers()

        def log_message(self, *_):
            pass
    return Handler


def session_path(path):
    """Split /sessions/<source>/<session>/<action> into its parts, or None."""
    parts = path.split('/')
    if len(parts) >= 5 and parts[:2] == ['', 'sessions'] and IDENTITY.fullmatch(parts[2]) and IDENTITY.fullmatch(parts[3]):
        return parts[2], parts[3], '/'.join(parts[4:])
    return None


def bare_project(project):
    return isinstance(project, str) and 0 < len(project.strip()) <= 80 and not any(sep in project for sep in ('/', '\\'))


def start_offers(starts):
    """Validate a runner's startable projects: bare names and known agents only."""
    if not isinstance(starts, list) or len(starts) > START_LIMIT:
        raise ValueError('invalid starts')
    offers = []
    for entry in starts:
        agents = entry.get('agents') if isinstance(entry, dict) else None
        if not bare_project(entry.get('project') if isinstance(entry, dict) else None) or not isinstance(agents, list) \
                or not agents or any(agent not in JOB_AGENTS for agent in agents):
            raise ValueError('invalid starts')
        offers.append({'project': entry['project'].strip(), 'agents': sorted(set(agents))})
    return offers


def host_job_path(path):
    """Split /hosts/<machine>/jobs[...job routes] into parts, or None (#19)."""
    parts = [unquote(p) for p in path.rstrip('/').split('/')]
    if len(parts) in (4, 5, 6) and parts[1] == 'hosts' and parts[3] == 'jobs' and MACHINE.fullmatch(parts[2]):
        return parts
    return None


def host_resume_path(path):
    """Split /hosts/<machine>/resume/claim into parts, or None (#18)."""
    parts = [unquote(p) for p in path.rstrip('/').split('/')]
    if len(parts) == 5 and parts[1] == 'hosts' and parts[3:] == ['resume', 'claim'] and MACHINE.fullmatch(parts[2]):
        return parts
    return None


def mqtt(journal, broker, stop, port=1883):
    while not stop.is_set():
        proc = None
        try:
            # Persistent QoS 1 subscription queues events while this collector is offline.
            proc = subprocess.Popen(['mosquitto_sub', '-c', '-i', 'axon-activity-journal', '-h', broker,
                '-p', str(port), '-t', 'axon/activity/+/+/events', '-t', 'axon/resources/+/+', '-q', '1', '-F', '%j'], stdout=subprocess.PIPE)
            pending = b''
            while not stop.is_set() and proc.poll() is None:
                if not select.select([proc.stdout], [], [], 0.5)[0]:
                    continue
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                pending += chunk
                while b'\n' in pending:
                    line, pending = pending.split(b'\n', 1)
                    try:
                        envelope = json.loads(line)
                        topic = envelope['topic'].split('/')
                        event = json.loads(envelope['payload'])
                        if len(topic)==4 and topic[:2]==['axon','resources']:
                            if topic[2:]!=[event.get('provider'),event.get('account')]:
                                raise ValueError('resource topic mismatch')
                            journal.ingest_resource(event)
                            continue
                        if len(topic) != 5 or topic[2:4] != [event.get('source'), event.get('session')]:
                            raise ValueError('topic identity mismatch')
                        journal.ingest(event)
                    except (ValueError, KeyError, TypeError, AttributeError):
                        logging.warning('Rejected malformed activity event')
                if len(pending) > 65536:
                    pending = b''
        except OSError:
            logging.exception('MQTT subscriber unavailable')
        finally:
            if proc:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait()
                proc.stdout.close()
        stop.wait(5)


def publish_view(journal, broker, stop, port=1883):
    last = None
    last_sent = 0
    while not stop.is_set():
        sessions = journal.sessions()
        # Terminal history stays queryable, while the tiny display shows the latest 24h.
        sessions = [s for s in sessions if time.time() - s['updated_at'] < 86400]
        payload = json.dumps({'version': 1, 'sessions': sessions})
        if payload != last or time.monotonic() - last_sent >= 30:
            try:
                result = subprocess.run(['mosquitto_pub', '-h', broker, '-p', str(port), '-t', 'axon/activity/view', '-r', '-q', '1', '-m', payload], timeout=3, capture_output=True)
                if result.returncode == 0:
                    last = payload
                    last_sent = time.monotonic()
            except (OSError, subprocess.TimeoutExpired):
                logging.warning('Unable to publish activity view')
        stop.wait(5)


def inference_endpoints(values):
    """Turn repeated --inference label=url options into a label/URL map."""
    endpoints = {}
    for value in values:
        label, separator, url = value.partition('=')
        if not separator or not INFERENCE_LABEL.fullmatch(label):
            raise ValueError('--inference expects label=url')
        location = urlsplit(url)
        if location.scheme not in ('http', 'https') or not location.netloc:
            raise ValueError('--inference URL must be a complete http(s) URL')
        if label in endpoints:
            raise ValueError(f'duplicate inference server label: {label}')
        endpoints[label] = url
    return endpoints


GPU_HOLD = 60


def held_gpus(journal, label, hold=GPU_HOLD):
    """Polling cannot see hardware, so carry a host collector's readings forward while they are
    fresh rather than blanking a working GPU bar every interval. Stale hardware goes unsaid."""
    prior = next((row for row in journal.resources() if row['provider'] == 'inference' and row['account'] == label), None)
    if not prior or not prior.get('gpus'):
        return []
    return prior['gpus'] if 0 <= time.time() - prior['observed_at'] <= hold else []


# llama.cpp-style servers reset their tokens-per-second gauges on every /metrics read, so a second
# reader beside Prometheus garbles both. Rates over the running counters stay right for any reader.
PROMETHEUS_LIVE = {
    'running': 'sum(llamacpp:requests_processing{{{sel}}})',
    'queued': 'sum(llamacpp:requests_deferred{{{sel}}})',
    'generation_tps': 'sum(rate(llamacpp:tokens_predicted_total{{{sel}}}[1m]))/sum(rate(llamacpp:tokens_predicted_seconds_total{{{sel}}}[1m]))',
    'prompt_tps': 'sum(rate(llamacpp:prompt_tokens_total{{{sel}}}[1m]))/sum(rate(llamacpp:prompt_seconds_total{{{sel}}}[1m]))',
    'kv_cache_percent': '100*max(llamacpp:kv_cache_usage_ratio{{{sel}}})',
    'device_losses': 'sum(gufo_device_lost_total{{{sel}}})',
}
# Each trend is one aggregate per step: peaks for load, averages for hardware, so a step never hides a burst.
AMD_HWMON = '* on(instance,chip) group_left node_hwmon_chip_names{{chip_name="amdgpu"}}'
PROMETHEUS_HISTORY = {
    'output_tps': 'sum(rate(llamacpp:tokens_predicted_total{{{sel}}}[{window}]))',
    'running': 'sum(max_over_time(llamacpp:requests_processing{{{sel}}}[{window}]))',
    'queued': 'sum(max_over_time(llamacpp:requests_deferred{{{sel}}}[{window}]))',
    'kv_cache_percent': '100*max(max_over_time(llamacpp:kv_cache_usage_ratio{{{sel}}}[{window}]))',
    'gpu_busy_percent': 'avg(avg_over_time(node_drm_gpu_busy_percent{{{sel}}}[{window}]))',
    'gpu_power_watts': 'max(avg_over_time(node_hwmon_power_average_watt{{{sel}}}[{window}]) '+AMD_HWMON+')',
    'gpu_temperature_c': 'max(avg_over_time(node_hwmon_temp_celsius{{{sel}}}[{window}]) '+AMD_HWMON+')',
}
HISTORY_RANGE = (900, 30*86400)
HISTORY_POINTS = 120


class Unscraped(LookupError):
    """Prometheus answers but holds no fresh sample of this server."""


def prometheus(base, path, params, timeout=4):
    """One Prometheus API call; labels are already restricted to INFERENCE_LABEL, so selectors stay literal."""
    with urlopen(base.rstrip('/')+path+'?'+urlencode(params), timeout=timeout) as response:
        body = json.loads(response.read(8388608).decode('utf-8', 'replace'))
    if body.get('status') != 'success':
        raise ValueError('Prometheus refused the query')
    return body['data']['result']


def finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float('inf'), float('-inf')) else None


def prometheus_metrics(base, label, timeout=4):
    """Read one server's live load from Prometheus. No series means Prometheus has no fresh scrape of it."""
    sel = f'instance="{label}"'
    metrics = {}
    for field, query in PROMETHEUS_LIVE.items():
        result = prometheus(base, '/api/v1/query', {'query': query.format(sel=sel)}, timeout)
        if not result:
            if field == 'running':
                raise Unscraped(label)
            continue
        value = finite(result[0]['value'][1])
        # 0/0 is a minute with nothing generated, which the endpoint itself reports as zero.
        if value is None and field.endswith('_tps'):
            value = 0.0
        if value is not None and value >= 0:
            metrics[field] = min(value, 100.0) if field.endswith('_percent') else value
    return metrics


def inference_history(base, label, seconds, end=None, timeout=8):
    """Trends for one server over the requested span, at most HISTORY_POINTS steps each."""
    seconds = max(HISTORY_RANGE[0], min(HISTORY_RANGE[1], int(seconds)))
    step = max(15, seconds // HISTORY_POINTS)
    end = int(end or time.time()) // step * step
    window = f'{max(step, 60)}s'
    sel = f'instance="{label}"'
    series = {}
    for name, query in PROMETHEUS_HISTORY.items():
        result = prometheus(base, '/api/v1/query_range', {'query': query.format(sel=sel, window=window),
            'start': end-seconds, 'end': end, 'step': step}, timeout)
        if result:
            series[name] = [[float(at), finite(value)] for at, value in result[0]['values']]
    totals = {}
    for name, counter in (('input', 'prompt_tokens_total'), ('output', 'tokens_predicted_total')):
        result = prometheus(base, '/api/v1/query', {'query': f'sum(increase(llamacpp:{counter}{{{sel}}}[{seconds}s]))', 'time': end}, timeout)
        if result and (value := finite(result[0]['value'][1])) is not None:
            totals[name] = round(value)
    return {'account': label, 'range': seconds, 'step': step, 'series': series, 'tokens': totals}


def endpoint_metrics(url, timeout=4):
    with urlopen(url, timeout=timeout) as response:
        return parse_inference_metrics(response.read(1048576).decode('utf-8', 'replace'))


def poll_inference(journal, endpoints, stop, interval=10, timeout=4, source=None):
    """Pull what each inference server already reports about itself; nothing runs on that host.
    With a Prometheus source, read it there and touch the endpoint only when Prometheus cannot answer."""
    while not stop.is_set():
        for label, url in endpoints.items():
            online, metrics = False, {}
            try:
                metrics = prometheus_metrics(source, label, timeout) if source else endpoint_metrics(url, timeout)
                online = True
            except (OSError, ValueError, LookupError, TypeError) as exc:
                if source and not isinstance(exc, Unscraped):
                    logging.warning('Prometheus could not report %s; asking the server directly', label)
                if source:
                    try:
                        metrics = endpoint_metrics(url, timeout); online = True
                    except (OSError, ValueError):
                        pass
                if not online:
                    # Requested endpoint failing is normal operation; the URL may carry a token, so log the label.
                    logging.warning('Inference server %s is not serving usable metrics', label)
            try:
                journal.ingest_resource(dict(version=1, provider='inference', account=label,
                    observed_at=time.time(), online=online, metrics=metrics, gpus=held_gpus(journal, label)))
            except ValueError:
                logging.warning('Rejected inference snapshot for %s', label)
        stop.wait(interval)


def maintain(journal, stop):
    # Startup pruning runs in Journal(); repeat even when no events arrive.
    while not stop.wait(3600):
        try:
            removed = journal.prune()
            if any(removed.values()):
                logging.info('Retention removed %s', removed)
        except sqlite3.Error:
            logging.exception('Journal retention failed; retrying next hour')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db', default='activity.sqlite3')
    p.add_argument('--broker', default=os.environ.get('AXON_BROKER', 'localhost'))
    p.add_argument('--broker-port', type=int, default=os.environ.get('AXON_PORT', '1883'))
    p.add_argument('--listen', default='127.0.0.1')
    p.add_argument('--port', type=int, default=8510)
    p.add_argument('--retention-days', type=int, default=30, help='History lifetime in days (default 30; 0 disables cleanup)')
    p.add_argument('--no-mqtt', action='store_true')
    p.add_argument('--no-web', action='store_true', help='Disable the browser UI')
    p.add_argument('--require-host-credentials', action='store_true',
                   help='Require host credentials (now the default)')
    p.add_argument('--allow-legacy-claims', action='store_true',
                   help='Temporary migration exception for in-turn claims; never allows unattended wake')
    p.add_argument('--input-token-file', default=os.environ.get('AXON_INPUT_TOKEN_FILE'),
                   help='File holding the bearer token that allows sending input to sessions; input is disabled without it')
    p.add_argument('--inference', action='append', default=[], metavar='LABEL=URL',
                   help='Poll the metrics an inference server exposes and report them as LABEL; repeat per server')
    p.add_argument('--inference-interval', type=float, default=10,
                   help='Seconds between inference polls (default 10; minimum 5)')
    p.add_argument('--prometheus', metavar='URL',
                   help='Prometheus that scrapes the --inference servers under instance=LABEL; read live load there and serve its history')
    p.add_argument('--updater', type=Path, default=os.environ.get('AXON_UPDATER_ROOT'),
                   help='Deploy root kept by axon_update.py; enables GET /update and token-protected POST /update')
    args = p.parse_args()
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    if not 1 <= args.broker_port <= 65535:
        p.error('--broker-port must be between 1 and 65535')
    if args.retention_days < 0:
        p.error('--retention-days must be nonnegative')
    try:
        inference = inference_endpoints(args.inference)
    except ValueError as exc:
        p.error(str(exc))
    if args.prometheus and (urlsplit(args.prometheus).scheme not in ('http', 'https') or not urlsplit(args.prometheus).netloc):
        p.error('--prometheus must be a complete http(s) URL')
    if not args.inference_interval >= 5:
        p.error('--inference-interval must be at least 5 seconds')
    token = Path(args.input_token_file).read_text().strip() if args.input_token_file else None
    if args.input_token_file and len(token or '') < 16:
        p.error('--input-token-file must hold a token of at least 16 characters')
    journal = Journal(args.db, args.retention_days)
    stop = threading.Event()
    maintenance = threading.Thread(target=maintain, args=(journal, stop), daemon=True)
    maintenance.start()
    threads = [maintenance]
    if not args.no_mqtt:
        for target in (mqtt, publish_view):
            thread = threading.Thread(target=target, args=(journal, args.broker, stop, args.broker_port), daemon=True)
            thread.start(); threads.append(thread)
    if inference:
        poller = threading.Thread(target=poll_inference, args=(journal, inference, stop, args.inference_interval, 4, args.prometheus), daemon=True)
        poller.start(); threads.append(poller)
        logging.info('Polling inference servers%s: %s', ' through Prometheus' if args.prometheus else '', ', '.join(sorted(inference)))
    server = ThreadingHTTPServer((args.listen, args.port), handler(journal, web=not args.no_web, token=token, stop=stop, updater=args.updater,
        prometheus_url=args.prometheus, inference=inference, allow_legacy_claims=args.allow_legacy_claims and not args.require_host_credentials))
    def shutdown(*_):
        stop.set()
        journal.changed()  # release open streams
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever()
    finally:
        stop.set(); server.server_close()
        for thread in threads:
            thread.join(timeout=4)


if __name__ == '__main__':
    main()
