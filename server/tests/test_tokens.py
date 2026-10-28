import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid
from axon_protocol import validate
from axon_reporter import report
from axon_reporter.tokens import session_tokens
from axon_server.journal import Journal


def claude_line(message, output, fresh=2, written=100, read=1000, text='private prompt text'):
    return json.dumps({'type': 'assistant', 'requestId': 'r-'+message, 'message': {'id': message, 'content': [{'type': 'text', 'text': text}],
        'usage': {'input_tokens': fresh, 'cache_creation_input_tokens': written, 'cache_read_input_tokens': read, 'output_tokens': output}}})+'\n'


def codex_line(total_in, cached, out):
    return json.dumps({'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'total_token_usage':
        {'input_tokens': total_in, 'cached_input_tokens': cached, 'output_tokens': out, 'reasoning_output_tokens': 7}}}})+'\n'


class TranscriptTokenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = patch.dict(os.environ, {'AXON_ACTIVITY_HOME': str(self.root/'state')}); patcher.start(); self.addCleanup(patcher.stop)

    def test_claude_counts_each_message_once_and_resumes(self):
        path = self.root/'claude.jsonl'
        # One message is written as several content lines repeating its usage; the last line is final.
        path.write_text(json.dumps({'type': 'user', 'message': {'content': 'hello usage'}})+'\n'+claude_line('m1', 10)+claude_line('m1', 40))
        self.assertEqual(session_tokens('claude', 's1', str(path)), {'input': 1102, 'cached': 1000, 'output': 40})
        with path.open('a') as handle: handle.write(claude_line('m1', 55)+claude_line('m2', 5)+'{"type":"assistant"')
        self.assertEqual(session_tokens('claude', 's1', str(path)), {'input': 2204, 'cached': 2000, 'output': 60},
                         'a later line of the same message replaces its count; a half-written line waits')
        state = sqlite3.connect(self.root/'state/outbox.sqlite3').execute('SELECT state FROM hook_tokens').fetchone()[0]
        self.assertNotIn('private prompt', state)

    def test_codex_reports_its_own_running_total(self):
        path = self.root/'rollout.jsonl'
        path.write_text(codex_line(500, 400, 20)+json.dumps({'type': 'response_item', 'payload': {'text': 'usage'}})+'\n'+codex_line(900, 700, 35))
        self.assertEqual(session_tokens('codex', 's2', str(path)), {'input': 900, 'cached': 700, 'output': 35})

    def test_unreadable_or_foreign_transcripts_give_nothing(self):
        self.assertIsNone(session_tokens('claude', 's', str(self.root/'missing.jsonl')))
        (self.root/'notes.txt').write_text(claude_line('m', 5))
        self.assertIsNone(session_tokens('claude', 's', str(self.root/'notes.txt')))
        (self.root/'x.jsonl').write_text(claude_line('m', 5))
        self.assertIsNone(session_tokens('hermes', 's', str(self.root/'x.jsonl')))

    def test_a_replaced_transcript_starts_over(self):
        path = self.root/'c.jsonl'; path.write_text(claude_line('m1', 10)+claude_line('m2', 10))
        session_tokens('claude', 's', str(path))
        path.write_text(claude_line('m3', 3))
        self.assertEqual(session_tokens('claude', 's', str(path))['output'], 3)

    def test_hook_events_carry_counts(self):
        path = self.root/'c.jsonl'; path.write_text(claude_line('m1', 12))
        sent = []
        with patch.object(report, 'report', side_effect=lambda agent, session, kind, patch: sent.append(patch)):
            report.hook({'session_id': 'abc', 'hook_event_name': 'Stop', 'cwd': '/tmp/p', 'transcript_path': str(path)}, 'claude')
            report.hook({'session_id': 'abc', 'hook_event_name': 'SessionStart', 'transcript_path': str(path)}, 'claude')
        self.assertEqual(sent[0]['tokens'], {'input': 1102, 'cached': 1000, 'output': 12})
        self.assertNotIn('tokens', sent[1])


class ProtocolTokenTests(unittest.TestCase):
    def event(self, tokens):
        return dict(version=1, source='claude-x', session='s', id='e', at=time.time(), kind='heartbeat', data={'tokens': tokens})

    def test_counts_are_validated(self):
        validate(self.event({'input': 10, 'cached': 4, 'output': 2}))
        for bad in ({'input': 1, 'output': 2}, {'input': 1, 'cached': 0, 'output': -1}, {'input': 1, 'cached': 2, 'output': 0},
                    {'input': 1.5, 'cached': 0, 'output': 0}, {'input': True, 'cached': 0, 'output': 0}, {'input': 1, 'cached': 0, 'output': 0, 'text': 1}):
            with self.assertRaises(ValueError): validate(self.event(bad))


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.journal = Journal(Path(self.tmp.name)/'journal.db'); self.now = time.time()

    def report(self, agent, session, ago, tokens=None, kind='heartbeat'):
        data = {'agent': agent, 'machine': 'm'}
        if tokens: data['tokens'] = dict(zip(('input', 'cached', 'output'), tokens))
        self.journal.ingest(dict(version=1, source=agent+'-x', session=session, id=uuid.uuid4().hex, at=self.now-ago, kind=kind, data=data))

    def test_period_counts_growth_per_agent(self):
        self.report('claude', 'old', 7200, (100, 50, 10), 'start')
        self.report('claude', 'old', 1800, (300, 150, 30))
        self.report('claude', 'old', 600, (1000, 800, 70))
        self.report('claude', 'new', 900, (40, 0, 4), 'start')
        self.report('codex', 'gone', 7200, (999, 0, 9), 'start')
        usage = self.journal.token_usage(self.now-3600)
        self.assertEqual(usage, {'claude': {'input': 940, 'cached': 750, 'output': 64, 'sessions': 2}})

    def test_a_session_already_running_counts_from_its_first_count_in_the_period(self):
        """Turning token reporting on must not dump a whole long session into the last hour."""
        self.report('codex', 's', 7200, kind='start')
        self.report('codex', 's', 1200, (10**8, 9*10**7, 10**5))
        self.report('codex', 's', 60, (10**8+500, 9*10**7+400, 10**5+20))
        self.assertEqual(self.journal.token_usage(self.now-3600)['codex'], {'input': 500, 'cached': 400, 'output': 20, 'sessions': 1})

    def test_a_count_that_went_backwards_is_new_work(self):
        self.report('claude', 's', 7200, (1000, 0, 100), 'start')
        self.report('claude', 's', 60, (30, 0, 3))
        self.assertEqual(self.journal.token_usage(self.now-3600)['claude']['output'], 3)
