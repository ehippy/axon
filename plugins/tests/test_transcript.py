import json
from pathlib import Path
import sys
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
for project in ('packages/python/axon_protocol', 'packages/python/axon_reporter', 'server', 'plugins/tools'):
    sys.path.insert(0, str(ROOT / project))
import manage
from axon_reporter import report, transcript
from axon_server.journal import Journal, handler
import test_plugins


def line(kind, uid, content=None, at='2026-10-30T18:35:19.939Z', **extra):
    return json.dumps({'type': kind, 'uuid': uid, 'timestamp': at, **({'message': {'role': kind, 'content': content}} if content is not None else {}), **extra})


SESSION_LINES = [
    line('user', 'u0', '<command-name>/clear</command-name>'),
    line('user', 'u1', 'Please fix the chat view'),
    line('assistant', 'a1', [{'type': 'thinking', 'thinking': 'PRIVATE THINKING'}]),
    line('assistant', 'a2', [{'type': 'text', 'text': 'Reading the dialog code.'}]),
    line('assistant', 'a3', [{'type': 'tool_use', 'id': 't1', 'name': 'Bash', 'input': {'command': 'PRIVATE COMMAND'}}]),
    line('user', 'r1', [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'PRIVATE OUTPUT'}]),
    line('user', 'm1', 'PRIVATE INJECTED', isMeta=True),
    line('assistant', 's1', [{'type': 'text', 'text': 'PRIVATE SUBAGENT'}], isSidechain=True),
    line('attachment', 'q1', attachment={'type': 'queued_command', 'prompt': 'also check the rebase'}),
    line('attachment', 'q2', attachment={'type': 'queued_command', 'prompt': '<task-notification>PRIVATE NOTICE</task-notification>'}),
    line('user', 'n1', '<task-notification>dashboard input already in the inbox</task-notification>'),
    line('assistant', 'a4', [{'type': 'text', 'text': 'Done; token AKIAIOSFODNN7EXAMPLE was in the log.'}]),
]


class ExtractionTests(unittest.TestCase):
    def test_only_prompts_and_replies_are_kept(self):
        found = [entry for raw in SESSION_LINES for entry in transcript.messages(json.loads(raw))]
        self.assertEqual([(role, text) for _, _, role, text in found], [
            ('user', 'Please fix the chat view'), ('assistant', 'Reading the dialog code.'),
            ('user', 'also check the rebase'), ('assistant', 'Done; token AKIAIOSFODNN7EXAMPLE was in the log.')])
        self.assertNotIn('PRIVATE', json.dumps(found))

    def test_scan_resumes_and_waits_for_a_complete_line(self):
        with self.subTest(), __import__('tempfile').TemporaryDirectory() as tmp:
            path = Path(tmp) / 'session.jsonl'
            path.write_text('\n'.join(SESSION_LINES[:4]) + '\n' + SESSION_LINES[4][:20])
            found, offset = transcript.scan(path, 0)
            self.assertEqual([text for *_, text in found], ['Please fix the chat view', 'Reading the dialog code.'])
            self.assertEqual(transcript.scan(path, offset), ([], offset))


class RedactionTests(unittest.TestCase):
    def test_secret_shapes_are_masked(self):
        for text in ('AKIAIOSFODNN7EXAMPLE', 'ghp_' + 'a1B2' * 9, 'sk-ant-api03-' + 'Xy9_' * 8, 'AIza' + 'A1b_' * 8 + 'xyz',
                     'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U',
                     '-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n-----END OPENSSH PRIVATE KEY-----',
                     'Zx8kQ2mP9vL4tR7wN1yB6cH3jF5gD0sA'):
            self.assertEqual(transcript.redact('key ' + text + ' here'), 'key [redacted] here')
        self.assertEqual(transcript.redact('Authorization: Bearer abcDEF123456789xyz'), 'Authorization: Bearer [redacted]')
        self.assertEqual(transcript.redact('postgres://admin:hunter2pass@db/x'), 'postgres://admin:[redacted]@db/x')
        self.assertEqual(transcript.redact("password = 'correct horse'"), "password = '[redacted]'")
        self.assertEqual(transcript.redact('export GITHUB_TOKEN=abcdef123456'), 'export GITHUB_TOKEN=[redacted]')
        self.assertEqual(transcript.redact('it said plain-words-123 here', ['plain-words-123']), 'it said [redacted] here')

    def test_ordinary_text_survives(self):
        for text in ('Pushed as 017b4bf, full a1d7cf252199cabdd0554ac4765b0c481099087e',
                     'session 0b1727b0-187a-4d56-859d-33017a7185ad', 'the input token never leaves it',
                     'test_consent_and_matching_agent_are_required_without_consuming_input',
                     '/home/user/.claude/projects/-home-user-Projects-axon/x.jsonl', 'token usage: 1234 tokens',
                     'set AXON_TRANSCRIPT=claude in ~/.config/axon/env', 'the passwordless login flow'):
            self.assertEqual(transcript.redact(text), text)


class SyncTests(unittest.TestCase):
    """The Stop hook sends masked conversation text to a real journal, only when opted in."""
    hook = test_plugins.PluginTests.hook
    clean_axon_env = test_plugins.InboxTests.clean_axon_env
    setUp = test_plugins.InboxTests.setUp

    def session(self):
        path = self.root / 'session.jsonl'
        path.write_text('\n'.join(SESSION_LINES) + '\n')
        with self.clean_axon_env():
            source, key, _ = report.identity('claude', 'a')
        self.journal.ingest(dict(version=1, source=source, session=key, id='s-a', at=time.time(), kind='start', data={'agent': 'claude', 'machine': 'workstation'}))
        return path, source, key

    def test_off_unless_the_agent_is_opted_in(self):
        path, source, key = self.session()
        self.hook(self.package, 'Stop', transcript_path=str(path))
        self.assertEqual(self.journal.transcript(source, key), [])

    def test_stop_hook_sends_masked_prompts_and_replies_once(self):
        path, source, key = self.session()
        self.env['AXON_TRANSCRIPT'] = 'claude'
        self.hook(self.package, 'Stop', transcript_path=str(path))
        self.hook(self.package, 'Stop', transcript_path=str(path))
        stored = self.journal.transcript(source, key)
        self.assertEqual([(m['role'], m['text']) for m in stored], [
            ('user', 'Please fix the chat view'), ('assistant', 'Reading the dialog code.'),
            ('user', 'also check the rebase'), ('assistant', 'Done; token [redacted] was in the log.')])
        self.assertNotIn('PRIVATE', json.dumps(stored))
        with open(path, 'a') as handle:
            handle.write(line('assistant', 'a5', [{'type': 'text', 'text': 'One more thing.'}]) + '\n')
        self.hook(self.package, 'PostToolUse', transcript_path=str(path))
        self.assertEqual(self.journal.transcript(source, key)[-1]['text'], 'One more thing.')
        self.assertEqual(len(self.journal.transcript(source, key)), 5)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = __import__('tempfile').TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.journal = Journal(Path(self.tmp.name) / 'journal.db')
        self.journal.ingest(dict(version=1, source='test', session='s', id='start', at=time.time(), kind='start', data={'agent': 'claude', 'machine': 'host-a'}))
        self.credential = self.journal.mint_host_credential('host-a')['credential']
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.journal, token='t' * 20))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)

    def post(self, body, credential=None):
        request = Request(f'http://127.0.0.1:{self.server.server_port}/sessions/test/s/transcript', data=json.dumps(body).encode(),
                          headers={'Content-Type': 'application/json', **({'Authorization': 'Bearer ' + credential} if credential else {})})
        with urlopen(request) as response:
            return json.load(response)

    def test_only_the_owning_host_can_add_text(self):
        entry = {'id': 'x', 'at': time.time(), 'role': 'user', 'text': 'hi'}
        for credential in (None, 'wrong', self.journal.mint_host_credential('host-b')['credential']):
            with self.assertRaises(HTTPError) as caught:
                self.post({'messages': [entry]}, credential)
            self.assertEqual(caught.exception.code, 401)
        self.assertEqual(self.post({'messages': [entry, entry]}, self.credential), {'added': 1})
        with urlopen(f'http://127.0.0.1:{self.server.server_port}/sessions/test/s/transcript') as response:
            self.assertEqual([m['text'] for m in json.load(response)['messages']], ['hi'])

    def test_entries_are_validated(self):
        for bad in ({'messages': []}, {'messages': [{'id': 'x', 'at': 1, 'role': 'tool', 'text': 'out'}]},
                    {'messages': [{'id': 'x', 'at': 1, 'role': 'user', 'text': 'x' * 8001}]}):
            with self.assertRaises(HTTPError) as caught:
                self.post(bad, self.credential)
            self.assertEqual(caught.exception.code, 400)

    def test_text_expires_on_its_own_clock(self):
        now = time.time()
        self.journal.add_transcript('test', 's', self.credential, {'messages': [
            {'id': 'old', 'at': now - 8 * 86400, 'role': 'user', 'text': 'old'}, {'id': 'new', 'at': now, 'role': 'user', 'text': 'new'}]})
        self.journal.prune(now)
        self.assertEqual([m['id'] for m in self.journal.transcript('test', 's')], ['new'])
        Journal(Path(self.tmp.name) / 'journal.db', 0).prune(now + 8 * 86400)
        self.assertEqual(self.journal.transcript('test', 's'), [])


if __name__ == '__main__':
    unittest.main()
