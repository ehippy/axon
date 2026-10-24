#!/usr/bin/env python3
"""Axon's durable session journal. Standard library + Mosquitto CLI only."""
from contextlib import contextmanager
import argparse
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

STATES = {'working', 'waiting', 'blocked', 'finished', 'stopped', 'interrupted'}
FIELDS = {'agent': 80, 'machine': 80, 'project': 120, 'goal': 400, 'step': 400,
          'activity': 120, 'outcome': 1000, 'parent': 160, 'branch': 200, 'blocker': 400}
TERMINAL = {'finished', 'stopped', 'interrupted'}


def validate(v):
    if not isinstance(v, dict) or v.get('version') != 1:
        raise ValueError('version must be 1')
    for key in ('source', 'session', 'id'):
        if not isinstance(v.get(key), str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,128}', v[key]):
            raise ValueError('invalid ' + key)
    if v.get('kind') not in {'start', 'progress', 'heartbeat', 'waiting', 'blocked', 'finished', 'stopped', 'interrupted'}:
        raise ValueError('invalid kind')
    at = v.get('at')
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not 0 < at <= time.time() + 60:
        raise ValueError('invalid timestamp')
    patch = v.get('data', {})
    if not isinstance(patch, dict) or set(patch) - (set(FIELDS) | {'state', 'evidence', 'lease_seconds'}):
        raise ValueError('invalid data fields')
    for key, limit in FIELDS.items():
        if key in patch and (not isinstance(patch[key], str) or len(patch[key]) > limit):
            raise ValueError('invalid ' + key)
    if 'state' in patch and patch['state'] not in STATES:
        raise ValueError('invalid state')
    evidence = patch.get('evidence', [])
    if not isinstance(evidence, list) or len(evidence) > 12 or any(not isinstance(x, str) or not x.strip() or len(x) > 500 for x in evidence):
        raise ValueError('invalid evidence')
    lease = patch.get('lease_seconds', 900)
    if type(lease) is not int or not 30 <= lease <= 3600:
        raise ValueError('invalid lease')
    if v['kind']=='finished' or patch.get('state')=='finished':
        if not patch.get('outcome','').strip() or not evidence:
            raise ValueError('finished requires outcome and evidence')
    return v


class Journal:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.executescript('''
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY, source TEXT, session TEXT, id TEXT,
                at REAL, received REAL, kind TEXT, payload TEXT,
                UNIQUE(source, session, id));
              CREATE INDEX IF NOT EXISTS event_session ON events(source, session, seq);
              CREATE TABLE IF NOT EXISTS sessions (
                source TEXT, session TEXT, at REAL, payload TEXT,
                PRIMARY KEY(source, session));
            ''')

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
        v = validate(event)
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
            current.update(patch)
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
            rows = db.execute('SELECT payload FROM sessions ORDER BY at DESC').fetchall()
        result = []
        for row in rows:
            item = json.loads(row[0])
            item['fresh'] = time.time() - item['updated_at'] <= item['lease_seconds']
            item['effective_state'] = item['state'] if item['fresh'] or item['state'] in TERMINAL else 'lost_contact'
            if any(item.get(k if k != 'state' else 'effective_state') != v for k, v in filters.items() if k in ('source', 'session', 'project', 'agent', 'state')):
                continue
            if filters.get('q', '').lower() not in json.dumps(item).lower():
                continue
            result.append(item)
        return result

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
            clauses.append("EXISTS (SELECT 1 FROM sessions s WHERE s.source=events.source AND s.session=events.session AND json_extract(s.payload, '$.project')=?)")
            args.append(filters['project'])
        after = max(0, int(filters.get('after', 0)))
        limit = min(500, max(1, int(filters.get('limit', 100))))
        clauses.append('seq>?'); args.append(after)
        with self.connect() as db:
            rows = db.execute('SELECT * FROM events WHERE ' + ' AND '.join(clauses) + ' ORDER BY seq LIMIT ?', args + [limit]).fetchall()
        return [{'seq': r['seq'], 'received_at': r['received'], **json.loads(r['payload'])} for r in rows]


def handler(journal):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            filters = {k: v[-1] for k, v in parse_qs(url.query).items()}
            try:
                if url.path == '/health':
                    data = {'ok': True}
                elif url.path == '/sessions':
                    data = {'sessions': journal.sessions(filters)}
                elif url.path == '/events':
                    events = journal.events(filters)
                    data = {'events': events, 'next_after': events[-1]['seq'] if events else int(filters.get('after', 0))}
                else:
                    self.send_error(404); return
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers(); self.wfile.write(body)
            except (ValueError, TypeError):
                self.send_error(400, 'Invalid query')
        def log_message(self, *_):
            pass
    return Handler


def mqtt(journal, broker, stop, port=1883):
    while not stop.is_set():
        proc = None
        try:
            # Persistent QoS 1 subscription queues events while this collector is offline.
            proc = subprocess.Popen(['mosquitto_sub', '-c', '-i', 'axon-activity-journal', '-h', broker,
                '-p', str(port), '-t', 'axon/activity/+/+/events', '-q', '1', '-F', '%j'], stdout=subprocess.PIPE)
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


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db', default='activity.sqlite3')
    p.add_argument('--broker', default='localhost')
    p.add_argument('--broker-port', type=int, default=1883)
    p.add_argument('--listen', default='127.0.0.1')
    p.add_argument('--port', type=int, default=8510)
    p.add_argument('--no-mqtt', action='store_true')
    args = p.parse_args()
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    journal = Journal(args.db)
    stop = threading.Event()
    threads = []
    if not args.no_mqtt:
        for target in (mqtt, publish_view):
            thread = threading.Thread(target=target, args=(journal, args.broker, stop, args.broker_port), daemon=True)
            thread.start(); threads.append(thread)
    server = ThreadingHTTPServer((args.listen, args.port), handler(journal))
    def shutdown(*_):
        stop.set()
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
