"""Real broker test; skipped when Mosquitto is not installed."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
for project in ('packages/python/axon_protocol', 'packages/python/axon_reporter', 'server', 'clients/query', 'plugins/tools'):
    sys.path.insert(0, str(ROOT / project))
from axon_reporter import report, resources


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def wait_for(fn, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            result = fn()
            if result:
                return result
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(.05)
    raise AssertionError('timed out waiting for MQTT journal integration')


@unittest.skipUnless(all(shutil.which(x) for x in ('mosquitto', 'mosquitto_pub', 'mosquitto_sub')), 'Mosquitto CLI required')
class BrokerTests(unittest.TestCase):
    def test_live_delivery_and_collector_outage_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker_port, http_port = port(), port()
            config = Path(tmp) / 'mosquitto.conf'
            config.write_text(f'listener {broker_port} 127.0.0.1\nallow_anonymous true\n')
            broker = subprocess.Popen(['mosquitto', '-c', str(config)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            collector = None
            def start():
                return subprocess.Popen([sys.executable, str(ROOT / 'server/axon_server/journal.py'),
                    '--db', str(Path(tmp)/'journal.db'), '--broker', '127.0.0.1', '--broker-port', str(broker_port),
                    '--port', str(http_port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={**os.environ, 'PYTHONPATH': os.pathsep.join(str(ROOT/p) for p in ('server', 'packages/python/axon_protocol'))})
            def sessions():
                with urlopen(f'http://127.0.0.1:{http_port}/sessions', timeout=1) as response:
                    return json.load(response)['sessions']
            try:
                wait_for(lambda: socket.create_connection(('127.0.0.1', broker_port), timeout=.2).close() is None)
                collector = start()
                wait_for(lambda: urlopen(f'http://127.0.0.1:{http_port}/health', timeout=1).close() is None)
                # Ensure the persistent subscription has reached the broker before publishing.
                time.sleep(.3)
                with patch.dict(os.environ, AXON_BROKER='127.0.0.1', AXON_PORT=str(broker_port), AXON_ACTIVITY_HOME=tmp):
                    event = report.report('codex', 'integration-session', 'start', {'project':'axon', 'goal':'Build activity journal'})
                    row = wait_for(lambda: sessions())
                    self.assertEqual(row[0]['goal'], 'Build activity journal')
                    sample=resources.snapshot('codex',[dict(name='short',window_minutes=300,used_percent=37,resets_at=time.time()+3600)])
                    resources.publish(sample)
                    def quota():
                        with urlopen(f'http://127.0.0.1:{http_port}/resources',timeout=1) as response:
                            return next((r for r in json.load(response)['resources'] if r['provider']=='codex' and r['windows']),None)
                    self.assertEqual(wait_for(quota)['windows'][0]['used_percent'],37)
                    collector.terminate(); collector.wait(timeout=5)
                    report.report('codex', 'integration-session', 'finished', {'outcome':'Recovery verified', 'evidence':['test: real MQTT']})
                    collector = start()
                    row = wait_for(lambda: [s for s in sessions() if s['state'] == 'finished'])
                    self.assertEqual(row[0]['outcome'], 'Recovery verified')
                    self.assertEqual(row[0]['session'], event['session'])
                    with urlopen(f'http://127.0.0.1:{http_port}/events') as response:
                        self.assertEqual(len(json.load(response)['events']), 2)
                def completed_view():
                    result = subprocess.run(['mosquitto_sub', '-h', '127.0.0.1', '-p', str(broker_port), '-t', 'axon/activity/view', '-C', '1', '-W', '8'], capture_output=True, timeout=10, check=True)
                    value = json.loads(result.stdout)
                    return value if any(row['state']=='finished' for row in value['sessions']) else None
                view = wait_for(completed_view, seconds=12)
                self.assertEqual(view['version'], 1)
                projector = os.environ.get('AXON_HANDSET_PROJECTOR')
                if projector:
                    result = subprocess.run([projector],input=json.dumps(view),text=True,capture_output=True,check=True)
                    rows = json.loads(result.stdout)
                    self.assertEqual(rows[0]['state'], 'idle')
                    self.assertIn('Recovery verified', rows[0]['task'])
            finally:
                if collector and collector.poll() is None:
                    collector.terminate(); collector.wait(timeout=5)
                broker.terminate(); broker.wait(timeout=5)


class IngestLineTests(unittest.TestCase):
    """The subscriber thread must survive database errors (board went dark on Oct 30)."""
    def setUp(self):
        from axon_server import journal
        self.journal = journal

    def line(self, source='codex', session='s1'):
        payload = json.dumps({'source': source, 'session': session})
        return json.dumps({'topic': f'axon/activity/{source}/{session}/events', 'payload': payload}).encode()

    def test_locked_database_is_retried_until_stored(self):
        import sqlite3, threading
        calls = []
        class Store:
            def ingest(self, event):
                calls.append(event)
                if len(calls) < 3:
                    raise sqlite3.OperationalError('database is locked')
        with self.assertLogs(level='WARNING'):
            self.assertTrue(self.journal.ingest_line(Store(), self.line(), threading.Event(), retry_delay=0))
        self.assertEqual(len(calls), 3)

    def test_unexpected_error_skips_only_that_event(self):
        import threading
        class Store:
            def ingest(self, event):
                raise RuntimeError('boom')
        with self.assertLogs(level='ERROR'):
            self.assertFalse(self.journal.ingest_line(Store(), self.line(), threading.Event()))

    def test_stop_ends_retry(self):
        import sqlite3, threading
        stop = threading.Event(); stop.set()
        class Store:
            def ingest(self, event):
                raise sqlite3.OperationalError('database is locked')
        with self.assertLogs(level='WARNING'):
            self.assertFalse(self.journal.ingest_line(Store(), self.line(), stop))

    def test_identity_mismatch_is_rejected(self):
        import threading
        bad = json.dumps({'topic': 'axon/activity/codex/other/events', 'payload': json.dumps({'source': 'codex', 'session': 's1'})}).encode()
        with self.assertLogs(level='WARNING'):
            self.assertFalse(self.journal.ingest_line(object(), bad, threading.Event()))


if __name__ == '__main__':
    unittest.main()
