import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[3]
for package in ('server', 'packages/python/axon_protocol', 'packages/python/axon_reporter'):
    sys.path.insert(0, str(ROOT / package))
from axon_server.journal import Journal, handler
from axon_reporter import plugin_runtime, report

SHIM = """#!/usr/bin/env python3
import os, sys, pathlib
pathlib.Path(os.environ['FAKE_CODEX_LOG']).write_text('\\n'.join(sys.argv[1:]))
sys.exit(0 if os.environ.get('FAKE_CODEX_OK', '1') == '1' else 1)
"""


class CodexQueueWakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.j = Journal(self.root / 'journal.db')
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.j))
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        credential = self.root / 'credential'
        credential.write_text(self.j.mint_host_credential('host-a')['credential']); credential.chmod(0o600)
        self.shim = self.root / 'codex'; self.shim.write_text(SHIM); self.shim.chmod(0o755)
        self.log = self.root / 'codex-args'
        self.env = {**os.environ, 'AXON_CONFIG_FILE': str(self.root / 'absent'),
            'AXON_HOST_CREDENTIAL_FILE': str(credential), 'AXON_ACTIVITY_HOME': str(self.root / 'state'),
            'AXON_MACHINE': 'host-a', 'AXON_SOURCE': 'codex-test', 'AXON_WAKE': 'codex',
            'AXON_BROKER': '127.0.0.1:20999',
            'AXON_CODEX_BIN': str(self.shim), 'FAKE_CODEX_LOG': str(self.log),
            'AXON_SERVER': f'http://127.0.0.1:{self.server.server_port}'}
        with patch.dict(os.environ, self.env):
            self.source, self.key, _ = report.identity('codex', 'thread-uuid-1')
        self.j.ingest(dict(version=1, source=self.source, session=self.key, id='start',
                           at=time.time(), kind='start', data={'agent': 'codex', 'machine': 'host-a'}))
        self.j.set_wake(self.source, self.key, True)
        self.children = []
        self.addCleanup(self.clean)

    def clean(self):
        for child in self.children:
            if child.poll() is None: child.terminate()
            child.wait(timeout=5)
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.tmp.cleanup()

    def watcher(self):
        env = {**self.env, 'AXON_LISTEN_POLL': '1', 'AXON_LISTEN_SECONDS': '8',
               'PYTHONPATH': os.pathsep.join([str(ROOT/'packages/python/axon_reporter'), str(ROOT/'packages/python/axon_protocol')])}
        child = subprocess.Popen([sys.executable, '-m', 'axon_reporter.plugin_runtime',
                                  'queue-listen', 'codex'], env=env,
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, text=True)
        self.children.append(child)
        child.stdin.write(json.dumps({'session_id': 'thread-uuid-1'})); child.stdin.close()
        return child

    def wait_delivered(self):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if self.j.messages(self.source, self.key)[0]['delivered_at'] is not None: return
            time.sleep(0.05)
        self.fail('Queue acknowledgment did not reach the server')

    def test_queue_delivery_records_thread_and_message(self):
        with patch.dict(os.environ, self.env):
            self.assertTrue(plugin_runtime.queue_delivery('codex', 'thread-uuid-1',
                          [{'text': 'Check the failing test'}]))
        recorded = self.log.read_text()
        self.assertIn('queue', recorded); self.assertIn('--thread', recorded)
        self.assertIn('thread-uuid-1', recorded); self.assertIn('Check the failing test', recorded)

    def test_queue_failure_is_reported_as_not_accepted(self):
        with patch.dict(os.environ, {**self.env, 'FAKE_CODEX_OK': '0'}):
            self.assertFalse(plugin_runtime.queue_delivery('codex', 'thread-uuid-1',
                           [{'text': 'anything'}]))

    def test_dashboard_message_reaches_codex_queue_while_idle(self):
        self.j.send_message(self.source, self.key, 'Wake and summarize')
        self.watcher()
        self.wait_delivered()
        recorded = ''
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and 'Wake and summarize' not in recorded:
            recorded = self.log.read_text() if self.log.exists() else ''
            time.sleep(0.05)
        self.assertIn('--thread', recorded); self.assertIn('thread-uuid-1', recorded)
        self.assertIn('Wake and summarize', recorded)
        events = json.dumps(self.j.events())
        self.assertNotIn('Wake and summarize', events); self.assertNotIn('thread-uuid-1', events)
        self.assertIn('codex-queue', events)

    def test_failed_queue_call_keeps_the_message_queued(self):
        self.j.send_message(self.source, self.key, 'Still wanted later')
        env_patch = {**self.env, 'FAKE_CODEX_OK': '0'}
        child = subprocess.Popen([sys.executable, '-m', 'axon_reporter.plugin_runtime',
                                  'queue-listen', 'codex'],
                                 env={**env_patch, 'AXON_LISTEN_POLL': '1',
                                      'AXON_LISTEN_SECONDS': '3', 'PYTHONPATH': os.pathsep.join([str(ROOT/'packages/python/axon_reporter'), str(ROOT/'packages/python/axon_protocol')])},
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, text=True)
        self.children.append(child)
        child.stdin.write(json.dumps({'session_id': 'thread-uuid-1'})); child.stdin.close()
        time.sleep(2.5)
        self.assertIsNone(self.j.messages(self.source, self.key)[0]['delivered_at'])

    def test_watch_requires_host_opt_in(self):
        with patch.dict(os.environ, {**self.env, 'AXON_WAKE': ''}):
            child = subprocess.run([sys.executable, '-m', 'axon_reporter.plugin_runtime',
                                    'queue-listen', 'codex'],
                                   env={**self.env, 'AXON_WAKE': '',
                                        'PYTHONPATH': os.pathsep.join([str(ROOT/'packages/python/axon_reporter'), str(ROOT/'packages/python/axon_protocol')])},
                                   input=json.dumps({'session_id': 'thread-uuid-1'}),
                                   capture_output=True, text=True, timeout=10)
        # Without wake enabled the watcher exits promptly without consuming anything.
        self.assertEqual(child.returncode, 0)
        self.assertIsNone(self.j.messages(self.source, self.key)[0]['delivered_at'] if self.j.messages(self.source, self.key) else None)


if __name__ == '__main__':
    unittest.main()
