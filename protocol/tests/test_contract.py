import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
for project in ('packages/python/axon_protocol', 'packages/python/axon_reporter', 'server'):
    sys.path.insert(0, str(ROOT/project))
from axon_protocol import validate
from axon_server.journal import Journal
from axon_reporter import report


class ContractTests(unittest.TestCase):
    def fixture(self, name):
        return json.loads((ROOT/'protocol/fixtures'/f'{name}.json').read_text())

    def test_shared_fixtures_validate_and_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Journal(Path(tmp)/'journal.db', retention_days=0)
            for name in ('start', 'finished'):
                event = self.fixture(name)
                self.assertEqual(validate(event), event)
                journal.ingest(event)
            with self.assertRaises(ValueError):
                validate(self.fixture('invalid-finished'))
            expected = self.fixture('view')['sessions'][0]
            self.assertEqual(journal.sessions(), [expected])

    def test_reporter_identity_is_stable_private_and_explicitly_overridable(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'AXON_ACTIVITY_HOME':tmp}, clear=True):
            first = report.identity('codex', 'chat')
            self.assertEqual(report.identity('codex', 'chat'), first)
            self.assertRegex(first[2], r'^[0-9a-f]{16}$')
            self.assertEqual(first[0], 'codex-'+first[2])
            self.assertNotEqual(report.identity('hermes', 'chat')[0], first[0])
            with patch.dict(os.environ, AXON_MACHINE='example-installation', AXON_SOURCE='example-source'):
                self.assertEqual(report.identity('codex', 'chat')[::2], ('example-source','example-installation'))
            with patch.dict(os.environ, AXON_SOURCE='bad/topic'):
                with self.assertRaises(ValueError): report.identity('codex', 'chat')

    def test_reporter_uses_existing_outbox_without_aggregate_publication(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, AXON_ACTIVITY_HOME=tmp, AXON_MACHINE='example'):
            db = Path(tmp)/'outbox.sqlite3'
            with sqlite3.connect(db) as connection:
                connection.execute('CREATE TABLE outbox(seq INTEGER PRIMARY KEY, topic TEXT, payload TEXT)')
                connection.execute('INSERT INTO outbox(topic,payload) VALUES(?,?)', ('old-topic', '{}'))
            report.report('codex','chat','start',{'goal':'Check packaging'},flush=False)
            with sqlite3.connect(db) as connection:
                topics=[row[0] for row in connection.execute('SELECT topic FROM outbox ORDER BY seq')]
            self.assertEqual(topics[0], 'old-topic')
            self.assertEqual(topics[1].split('/')[:2], ['axon','activity'])
            self.assertFalse(hasattr(report, 'publish_summary'))

if __name__ == '__main__': unittest.main()
