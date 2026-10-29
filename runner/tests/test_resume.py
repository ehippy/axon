import fcntl
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
for package in ('server', 'packages/python/axon_protocol', 'runner'):
    sys.path.insert(0, str(ROOT/package))
from axon_server import journal as journal_module
from axon_server.journal import Journal, handler
from axon_runner import cli, config, resume

CLAUDE_ID = '0b9f8e1c-1d2e-4f3a-8b4c-5d6e7f8a9b0c'
CODEX_ID = '01a187e0-c041-72a3-8243-7a972b742cc4'

# Records argv one per line; `agents --json` prints the listing in FAKE_AGENTS.
FAKE = """#!/bin/sh
if [ "$1" = agents ]; then cat "$FAKE_AGENTS"; exit 0; fi
printf '%%s\\n' "$@" > %(trace)s
%(body)s
"""


class ResumeTests(unittest.TestCase):
    """Issue #18: input from the phone resumes a session that has ended on its host."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / 'project'; self.project.mkdir()
        env = patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.root / 'claude'),
                                      'CODEX_HOME': str(self.root / 'codex'),
                                      'FAKE_AGENTS': str(self.root / 'agents.json')})
        env.start(); self.addCleanup(env.stop)
        grace = patch.object(journal_module, 'RESUME_GRACE', 0); grace.start(); self.addCleanup(grace.stop)
        (self.root / 'agents.json').write_text('[]')
        self.journal = Journal(self.root / 'journal.db')
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.journal))
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start(); self.addCleanup(self.server.shutdown)
        self.base = 'http://127.0.0.1:%d' % self.server.server_address[1]
        self.credential = self.journal.mint_host_credential('runner-test')['credential']
        transcript = self.root / 'claude' / 'projects' / '-project' / (CLAUDE_ID + '.jsonl')
        transcript.parent.mkdir(parents=True)
        transcript.write_text('{"type":"summary"}\n' + json.dumps({'type': 'user', 'cwd': str(self.project)}) + '\n')
        rollout = self.root / 'codex' / 'sessions' / '2026' / '10' / '29' / ('rollout-2026-10-29T07-36-55-%s.jsonl' % CODEX_ID)
        rollout.parent.mkdir(parents=True)
        rollout.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': CODEX_ID, 'cwd': str(self.project)}}) + '\n')
        (self.root / 'codex' / 'thread-writer-locks').mkdir()

    def fake(self, name, body='exit 0'):
        (self.root / 'bin').mkdir(exist_ok=True)
        script = self.root / 'bin' / name
        script.write_text(FAKE % {'trace': self.root / (name + '.argv'), 'body': body})
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return str(script)

    def argv(self, name):
        path = self.root / (name + '.argv')
        return path.read_text().rstrip('\n').split('\n') if path.exists() else None

    def policy(self, resume_toml='[resume]\n', executable=None):
        path = self.root / 'runner.toml'
        executables = ''.join('%s="%s"\n' % item for item in (executable or {}).items())
        path.write_text('%s%s' % (resume_toml, ('[executable]\n' + executables) if executable else ''))
        return config.load(path)

    def dormant(self, agent, native, text='Carry on from the phone'):
        key = resume.session_key(native)
        self.journal.ingest(dict(version=1, source=agent + '-host', session=key, id='end', at=time.time() - 60,
                                 kind='stopped', data={'agent': agent, 'machine': 'runner-test'}))
        self.journal.set_wake(agent + '-host', key, True)
        self.journal.send_message(agent + '-host', key, text)
        return agent + '-host', key

    def poll(self, policy):
        logs = []
        return resume.run_once(self.base, 'runner-test', self.credential, policy, 0, cli.post,
                               log=logs.append, log_dir=self.root), logs

    def board(self, source, key):
        return self.journal.sessions({'source': source, 'session': key})[0]

    def test_exited_claude_session_resumes_in_its_folder_with_dont_ask(self):
        source, key = self.dormant('claude', CLAUDE_ID)
        fake = self.fake('claude', 'pwd > %s; exit 0' % (self.root / 'claude.pwd'))
        claimed, logs = self.poll(self.policy(executable={'claude': fake}))
        self.assertEqual(claimed['session'], key)
        argv = self.argv('claude')
        self.assertEqual(argv[:5], ['--bg', '--resume', CLAUDE_ID, '--permission-mode', 'dontAsk'])
        self.assertIn('Carry on from the phone', '\n'.join(argv[5:]))
        self.assertEqual(Path((self.root / 'claude.pwd').read_text().strip()).resolve(), self.project.resolve())
        self.assertEqual(self.journal.queued(), {})
        self.assertIn('claude-resume', json.dumps(self.journal.events({'session': key})))
        self.assertNotIn(CLAUDE_ID, ' '.join(logs))

    def test_open_claude_session_is_never_forked(self):
        source, key = self.dormant('claude', CLAUDE_ID)
        (self.root / 'agents.json').write_text(json.dumps([{'sessionId': CLAUDE_ID, 'kind': 'interactive'}]))
        fake = self.fake('claude')
        self.poll(self.policy(executable={'claude': fake}))
        self.assertIsNone(self.argv('claude'))
        self.assertEqual(self.journal.queued(), {(source, key): 1})
        self.assertEqual(self.board(source, key)['resume_hold']['reason'], 'session_open')
        self.assertIsNone(self.poll(self.policy(executable={'claude': fake}))[0])  # held, not retried

    def test_launch_failure_is_shown_and_input_kept(self):
        source, key = self.dormant('claude', CLAUDE_ID)
        fake = self.fake('claude', 'echo "Workspace not trusted. Run claude in /home/me/secret once \xe2\x9c\x97" >&2; exit 3')
        self.poll(self.policy(executable={'claude': fake}))
        self.assertEqual(self.journal.queued(), {(source, key): 1})
        self.assertEqual(self.board(source, key)['resume_hold']['reason'],
                         'launch_failed: Workspace not trusted. Run claude in <path> once')

    def test_bypass_permissions_is_refused(self):
        source, key = self.dormant('claude', CLAUDE_ID)
        fake = self.fake('claude')
        self.poll(self.policy('[resume]\npermission_mode="bypassPermissions"\n', executable={'claude': fake}))
        self.assertIsNone(self.argv('claude'))
        self.assertEqual(self.board(source, key)['resume_hold']['reason'], 'bad_permission_mode')

    def test_unknown_session_is_refused_not_dropped(self):
        source, key = self.dormant('claude', '11111111-2222-4333-8444-555555555555')
        self.poll(self.policy(executable={'claude': self.fake('claude')}))
        self.assertEqual(self.board(source, key)['resume_hold']['reason'], 'session_not_found')

    def test_unloaded_codex_thread_resumes_headless_in_the_workspace_sandbox(self):
        source, key = self.dormant('codex', CODEX_ID)
        fake = self.fake('codex', 'echo \'{"type":"thread.started"}\'; echo \'{"type":"turn.started"}\'; exit 0')
        self.poll(self.policy(executable={'codex': fake}))
        argv = self.argv('codex')
        self.assertEqual(argv[:6], ['exec', 'resume', '--json', '-c', 'sandbox_mode="workspace-write"', CODEX_ID])
        self.assertIn('Carry on from the phone', '\n'.join(argv[6:]))
        self.assertEqual(self.journal.queued(), {})
        self.assertIn('codex-resume', json.dumps(self.journal.events({'session': key})))

    def test_loaded_codex_thread_gets_the_input_through_its_own_queue(self):
        source, key = self.dormant('codex', CODEX_ID)
        lock = open(self.root / 'codex' / 'thread-writer-locks' / (CODEX_ID + '.lock'), 'w')
        self.addCleanup(lock.close)
        fcntl.flock(lock, fcntl.LOCK_EX)
        self.poll(self.policy(executable={'codex': self.fake('codex')}))
        self.assertEqual(self.argv('codex')[:3], ['queue', '--thread', CODEX_ID])
        self.assertEqual(self.journal.queued(), {})

    def test_resume_is_off_without_its_own_config_section(self):
        self.assertIsNone(self.policy('').resume)
        self.assertEqual(self.policy('[resume]\nagents=["codex","zelda"]\n').resume['agents'], ['codex'])

    def test_only_agents_the_host_enabled_are_claimed(self):
        source, key = self.dormant('claude', CLAUDE_ID)
        claimed, _ = self.poll(self.policy('[resume]\nagents=["codex"]\n'))
        self.assertIsNone(claimed)
        self.assertEqual(self.journal.queued(), {(source, key): 1})


if __name__ == '__main__':
    unittest.main()
