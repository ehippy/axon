import importlib.util
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
from urllib.request import urlopen
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[2]
for project in ('packages/python/axon_protocol', 'packages/python/axon_reporter', 'server', 'clients/query', 'plugins/tools'):
    sys.path.insert(0, str(ROOT / project))
from axon_server.journal import Journal, handler
from axon_reporter import report


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'journal.db'
        self.journal = Journal(self.db)
        self.at = time.time() - 10
        self.n = 0

    def tearDown(self):
        self.tmp.cleanup()

    def event(self, kind='progress', session='one', at=None, **data):
        self.n += 1
        return dict(version=1, source='codex-host', session=session, id=str(self.n),
                    kind=kind, at=at or self.at + self.n / 100, data=data)

    def test_separate_sessions_and_durable_history(self):
        self.journal.ingest(self.event('start', project='glass', goal='Fix calendar'))
        self.journal.ingest(self.event('start', session='two', project='axon', goal='Build journal'))
        self.journal.ingest(self.event('waiting', session='one'))
        restarted = Journal(self.db)
        self.assertEqual(len(restarted.sessions()), 2)
        self.assertEqual(restarted.sessions({'project': 'glass'})[0]['effective_state'], 'waiting')
        self.assertEqual(restarted.sessions({'project': 'axon'})[0]['effective_state'], 'working')
        self.assertEqual(len(restarted.events({'session': 'one'})), 2)

    def test_duplicate_and_out_of_order_delivery(self):
        newest = self.event('start', goal='New goal')
        self.assertTrue(self.journal.ingest(newest))
        self.assertFalse(self.journal.ingest(newest))
        self.journal.ingest(self.event('stopped', at=self.at - 20))
        self.assertEqual(self.journal.sessions()[0]['state'], 'working')
        self.assertEqual(len(self.journal.events()), 2)

    def test_completion_requires_evidence_and_survives_stop(self):
        with self.assertRaisesRegex(ValueError, 'evidence'):
            self.journal.ingest(self.event('finished', outcome='Done'))
        self.journal.ingest(self.event('finished', outcome='Fixed recurrence', evidence=['commit:abc', 'cargo test: passed']))
        self.journal.ingest(self.event('waiting', activity='awaiting input'))
        self.journal.ingest(self.event('stopped'))
        self.assertEqual(self.journal.sessions()[0]['state'], 'finished')
        self.journal.ingest(self.event('start', goal='Next task'))
        row = self.journal.sessions()[0]
        self.assertEqual(row['state'], 'working')
        self.assertNotIn('outcome', row)
        self.assertNotIn('evidence', row)
        self.assertNotIn('step', row)

    def test_expiry_is_not_completion_and_heartbeat_renews_lease(self):
        self.journal.ingest(self.event('start', at=self.at - 100, lease_seconds=30))
        self.assertEqual(self.journal.sessions()[0]['effective_state'], 'lost_contact')
        self.journal.ingest(self.event('heartbeat'))
        self.assertEqual(self.journal.sessions()[0]['effective_state'], 'working')
        self.journal.ingest(self.event('blocked', blocker='Need credentials'))
        self.assertEqual(self.journal.sessions({'state': 'blocked'})[0]['blocker'], 'Need credentials')

    def test_semantics_survive_generic_hook_activity(self):
        self.journal.ingest(self.event('start', project='glass', goal='Improve help', step='Catalog abilities'))
        self.journal.ingest(self.event('heartbeat', activity='reading'))
        row = self.journal.sessions()[0]
        self.assertEqual(row['step'], 'Catalog abilities')
        self.assertEqual(row['activity'], 'reading')

    def test_validation_and_pagination(self):
        for bad in [self.event('oops'), self.event(goal='x'*401), self.event(state='banana'),
                    self.event(at=time.time()+1000), self.event(lease_seconds=-1), self.event(evidence='commit')]:
            with self.assertRaises(ValueError):
                self.journal.ingest(bad)
        for _ in range(3):
            self.journal.ingest(self.event())
        first = self.journal.events({'limit': '2'})
        second = self.journal.events({'after': str(first[-1]['seq'])})
        self.assertEqual(len(second), 1)
        self.assertGreater(second[0]['seq'], first[-1]['seq'])

    def test_http_current_history_and_bad_queries(self):
        self.journal.ingest(self.event('start', project='glass'))
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.journal))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with urlopen(base + '/sessions?project=glass') as response:
                self.assertEqual(len(json.load(response)['sessions']), 1)
            with urlopen(base + '/events?limit=1') as response:
                self.assertEqual(json.load(response)['next_after'], 1)
            from urllib.error import HTTPError
            with self.assertRaises(HTTPError) as error:
                urlopen(base + '/events?limit=oops')
            self.assertEqual(error.exception.code, 400)
            error.exception.close()
        finally:
            server.shutdown(); server.server_close(); thread.join()


class ReporterTests(unittest.TestCase):
    def test_hooks_never_read_prompt_or_tool_arguments(self):
        with patch.object(report, 'report') as emit:
            report.hook(dict(session_id='private-id', cwd='/projects/glass', hook_event_name='PostToolUse',
                             tool_name='exec_command', prompt='SECRET', tool_input={'cmd':'SECRET'}), 'codex')
            args = emit.call_args.args
            self.assertEqual(args[2], 'heartbeat')
            self.assertNotIn('SECRET', json.dumps(args))
            self.assertEqual(args[3]['project'], 'glass')
            self.assertEqual(args[3]['state'], 'working')

    def test_outbox_survives_failure_and_retries_same_event_id(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, AXON_ACTIVITY_HOME=tmp), \
             patch.object(report.shutil, 'which', return_value='/bin/pub'), patch.object(report.subprocess, 'run') as run:
            run.return_value.returncode = 1
            original = report.report('codex', 'private-id', 'start', {'goal':'Build journal'})
            self.assertNotIn('private-id', original['session'])
            run.return_value.returncode = 0
            report.drain()
            payload = json.loads(run.call_args.args[0][run.call_args.args[0].index('-m')+1])
            self.assertEqual(payload['id'], original['id'])
            self.assertNotIn('-r', run.call_args.args[0])
            run.reset_mock()
            report.drain()
            run.assert_not_called()

    def test_stop_is_waiting_not_finished(self):
        with patch.object(report, 'report') as emit:
            report.hook(dict(session_id='one', hook_event_name='Stop'), 'claude')
            self.assertEqual(emit.call_args.args[2], 'waiting')


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'history.db'
        self.now = time.time()
    def tearDown(self):
        self.tmp.cleanup()
    def event(self, id, at, session='old', kind='progress', **data):
        return dict(version=1,source='test',session=session,id=id,at=at,kind=kind,data=data)
    def test_default_prunes_on_startup_but_preserves_active_session_goal(self):
        journal = Journal(self.path, retention_days=0)
        journal.ingest(self.event('1',self.now-40*86400,session='active',kind='start',goal='Long-running work'))
        journal.ingest(self.event('2',self.now,session='active',kind='heartbeat'))
        journal.ingest(self.event('3',self.now-31*86400))
        journal.ingest(self.event('4',self.now-86400,session='recent'))
        restarted = Journal(self.path)
        self.assertEqual(restarted.retention_days,30)
        self.assertEqual([e['id'] for e in restarted.events()],['2','4'])
        self.assertEqual({s['session'] for s in restarted.sessions()},{'active','recent'})
        self.assertEqual(restarted.sessions({'session':'active'})[0]['goal'],'Long-running work')
    def test_late_delivery_cannot_resurrect_expired_history(self):
        journal = Journal(self.path)
        self.assertFalse(journal.ingest(self.event('1',self.now-31*86400)))
        self.assertEqual(journal.events(),[])
        self.assertEqual(journal.sessions(),[])
    def test_custom_window_boundary_and_disabled_cleanup(self):
        journal=Journal(self.path,retention_days=0)
        journal.ingest(self.event('1',self.now-7*86400-1))
        journal.ingest(self.event('2',self.now-7*86400,session='boundary'))
        self.assertEqual(journal.prune(now=self.now),{'events':0,'sessions':0})
        journal.retention_days=7
        self.assertEqual(journal.prune(now=self.now),{'events':1,'sessions':1})
        self.assertEqual(journal.events()[0]['id'],'2')
        with self.assertRaises(ValueError): Journal(self.path,retention_days=-1)
    def test_pagination_cursor_never_reused_after_all_events_expire(self):
        journal=Journal(self.path,retention_days=0)
        journal.ingest(self.event('1',self.now-31*86400))
        cursor=journal.events()[-1]['seq']
        journal.retention_days=30
        journal.prune(now=self.now)
        journal.ingest(self.event('2',self.now,session='new'))
        result=journal.events({'after':str(cursor)})
        self.assertEqual(len(result),1)
        self.assertGreater(result[0]['seq'],cursor)
    def test_existing_database_migration_preserves_cursor_high_water(self):
        import sqlite3
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE events (seq INTEGER PRIMARY KEY,source TEXT,session TEXT,id TEXT,at REAL,received REAL,kind TEXT,payload TEXT,UNIQUE(source,session,id))')
            v=self.event('old',self.now-40*86400)
            db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?)',(41,'test','old','old',v['at'],self.now,'progress',json.dumps(v)))
        journal=Journal(self.path)
        self.assertEqual(journal.events(),[])
        journal.ingest(self.event('new',self.now))
        self.assertEqual(journal.events({'after':'41'})[0]['seq'],42)


if __name__ == '__main__':
    unittest.main()
