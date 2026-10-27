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
from axon_reporter import report


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


if __name__ == '__main__':
    unittest.main()
