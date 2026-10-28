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
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

from axon_protocol import STATES, FIELDS, TERMINAL, validate
from axon_protocol.resources import parse_inference_metrics, validate_resource

IDENTITY = re.compile(r'[a-zA-Z0-9_.-]{1,128}')
INFERENCE_LABEL = re.compile(r'[a-zA-Z0-9_.-]{1,80}')
MESSAGE_LIMIT = 4000
CLAIM_WAIT_LIMIT = 55
STREAM_PING = 15
UPDATE_UNIT = 'axon-update.service'
UPDATE_DEBOUNCE = 30


WEB_ASSETS = {'/': ('index.html', 'text/html; charset=utf-8'),
                          '/ui/app.css': ('app.css', 'text/css; charset=utf-8'),
                          '/ui/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                          '/ui/favicon.svg': ('favicon.svg', 'image/svg+xml'),
                          **{f'/ui/icons/{name}.svg': (f'icons/{name}.svg', 'image/svg+xml')
                             for name in ('laptop', 'pc-display', 'hdd-rack', 'chat-dots')}}


def server_revision():
    """Fingerprint code and shipped UI, independent of data and deployment paths."""
    digest = hashlib.sha256(Path(__file__).read_bytes())
    for name in sorted({asset[0] for asset in WEB_ASSETS.values()}):
        digest.update(name.encode())
        digest.update(files('axon_server').joinpath('web', *name.split('/')).read_bytes())
    return digest.hexdigest()


class Journal:
    def __init__(self, path, retention_days=30):
        if type(retention_days) is not int or retention_days < 0:
            raise ValueError('retention_days must be a nonnegative integer')
        self.retention_days = retention_days
        self.path = str(path)
        self.inbox = threading.Condition()
        self.changes = threading.Condition()
        self.version = 0
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
              CREATE INDEX IF NOT EXISTS message_session ON messages(source, session, at);
            ''')


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
            db.execute('DELETE FROM session_archives WHERE NOT EXISTS (SELECT 1 FROM sessions s WHERE s.source=session_archives.source AND s.session=session_archives.session)')
        if events or sessions:
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
            current.update({k: value for k, value in patch.items() if k != 'user_turn'})
            state = {'start': 'working', 'waiting': 'waiting', 'blocked': 'blocked',
                     'finished': 'finished', 'stopped': 'stopped', 'interrupted': 'interrupted'}.get(v['kind'])
            # Late lifecycle/tool hooks cannot erase an explicitly recorded result.
            if prior and json.loads(prior['payload']).get('state') == 'finished' and v['kind'] != 'start':
                current['state'] = 'finished'
            elif state:
                current['state'] = state
            current.setdefault('state', 'working')
            current.setdefault('lease_seconds', 900)
            current['updated_at'] = v['at']
            db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?,?,?)', (v['source'], v['session'], v['at'], json.dumps(current)))
        return True

    def sessions(self, filters=None):
        filters = filters or {}
        with self.connect() as db:
            rows = db.execute('SELECT s.payload, a.archived_at FROM sessions s LEFT JOIN session_archives a ON s.source=a.source AND s.session=a.session ORDER BY s.at DESC').fetchall()
        result = []
        for row in rows:
            item = json.loads(row[0])
            if row['archived_at'] is not None:
                item['archived_at'] = row['archived_at']
            item['fresh'] = time.time() - item['updated_at'] <= item['lease_seconds']
            item['effective_state'] = item['state'] if item['fresh'] or item['state'] in TERMINAL else 'lost_contact'
            if any(item.get(k if k != 'state' else 'effective_state') != v for k, v in filters.items() if k in ('source', 'session', 'project', 'agent', 'machine', 'state')):
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
        for field in ('project', 'agent', 'machine'):
            if filters.get(field):
                clauses.append("EXISTS (SELECT 1 FROM sessions s WHERE s.source=events.source AND s.session=events.session AND json_extract(s.payload, '$." + field + "')=?)")
                args.append(filters[field])
        if filters.get('q'):
            clauses.append('instr(lower(payload), lower(?))>0'); args.append(filters['q'])
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
            db.execute('INSERT INTO messages VALUES(:id,:source,:session,:at,:text,:delivered_at)', message)
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
            rows = db.execute('SELECT * FROM messages WHERE source=? AND session=? ORDER BY at', (source, session)).fetchall()
        return [dict(r) for r in rows]

    def claim(self, source, session, wait=0):
        """Atomically hand pending messages to the session's hook, waiting up to `wait` seconds."""
        if not (IDENTITY.fullmatch(source) and IDENTITY.fullmatch(session)):
            raise ValueError('invalid session identity')
        deadline = time.time() + min(CLAIM_WAIT_LIMIT, max(0, wait))
        while True:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                rows = [dict(r) for r in db.execute('SELECT * FROM messages WHERE source=? AND session=? AND delivered_at IS NULL ORDER BY at',
                                                    (source, session)).fetchall()]
                now = time.time()
                for row in rows:
                    row['delivered_at'] = now
                    db.execute('UPDATE messages SET delivered_at=? WHERE id=?', (now, row['id']))
            if rows:
                self.changed()  # queued counts on the board drop
            remaining = deadline - time.time()
            if rows or remaining <= 0:
                return rows
            with self.inbox:
                self.inbox.wait(remaining)


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


def handler(journal, web=True, token=None, stop=None, updater=None, prometheus_url=None, inference=()):
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
                    data = {'ok': True, 'retention_days': journal.retention_days}
                elif url.path == '/resources':
                    data = {'resources': journal.resources()}
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
            if not parts or parts[2] not in ('messages', 'inbox/claim', 'archive'):
                self.send_error(501); return
            try:
                if parts[2] in ('messages', 'archive'):
                    if not self.authorized('Session input is disabled'): return
                    length = int(self.headers.get('Content-Length', 0))
                    if not 0 < length <= 4 * MESSAGE_LIMIT + 1024:
                        raise ValueError('invalid body')
                    payload = json.loads(self.rfile.read(length))
                    if parts[2] == 'archive':
                        data, status = journal.archive(parts[0], parts[1], payload.get('archived')), 200
                    else:
                        data, status = journal.send_message(parts[0], parts[1], payload.get('text')), 201
                else:
                    wait = float(parse_qs(url.query).get('wait', ['0'])[-1])
                    data, status = {'messages': journal.claim(parts[0], parts[1], wait)}, 200
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers(); self.wfile.write(body)
            except LookupError:
                self.send_error(404, 'Unknown session')
            except (ValueError, TypeError, AttributeError):
                self.send_error(400, 'Invalid request')

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
    return {'account': label, 'range': seconds, 'step': step, 'series': series}


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
        prometheus_url=args.prometheus, inference=inference))
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
