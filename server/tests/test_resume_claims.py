import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
for package in ('server', 'packages/python/axon_protocol'):
    sys.path.insert(0, str(ROOT/package))
from axon_server import journal as journal_module
from axon_server.journal import Journal, handler


class ResumeClaimTests(unittest.TestCase):
    """Issue #18: a host runner resumes sessions nothing on the host is running."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.j = Journal(Path(self.tmp.name)/'journal.db')
        self.credential = self.j.mint_host_credential('host-a')['credential']
        self.other = self.j.mint_host_credential('host-b')['credential']
        grace = patch.object(journal_module, 'RESUME_GRACE', 0); grace.start(); self.addCleanup(grace.stop)

    def session(self, session='s', kind='finished', agent='claude', machine='host-a', at=None, **data):
        at = time.time() - 1000 if at is None else at
        extra = {'outcome': 'Done', 'evidence': ['test']} if kind == 'finished' else {}
        self.j.ingest(dict(version=1, source='src', session=session, id=session + kind + str(at), at=at, kind=kind,
                           data={'agent': agent, 'machine': machine, 'goal': 'Keep semantic work', **extra, **data}))

    def claim(self, agents=('claude', 'codex'), credential=None, machine='host-a'):
        return self.j.claim_resume(machine, credential or self.credential, agents)

    def ack(self, claimed, accepted=True, error=None):
        payload = {'ids': [m['id'] for m in claimed['messages']],
                   'claim_token': claimed['messages'][0]['claim_token'], 'accepted': accepted}
        if error:
            payload['error'] = error
        return self.j.acknowledge_wake(claimed['source'], claimed['session'], self.credential, payload)

    def test_dormant_session_with_consent_is_handed_to_its_own_host_only(self):
        self.session()
        self.j.send_message('src', 's', 'Pick this back up')
        self.assertIsNone(self.claim())  # wake consent off
        self.j.set_wake('src', 's', True)
        self.assertIsNone(self.claim(credential=self.other, machine='host-b'))
        with self.assertRaises(PermissionError):
            self.claim(credential=self.other)
        self.assertIsNone(self.claim(agents=('codex',)))
        claimed = self.claim()
        self.assertEqual((claimed['source'], claimed['session'], claimed['agent']), ('src', 's', 'claude'))
        self.assertEqual([m['text'] for m in claimed['messages']], ['Pick this back up'])
        self.assertIsNone(self.claim())  # leased
        self.ack(claimed)
        self.assertEqual(self.j.queued(), {})
        self.assertIn('claude-resume', json.dumps(self.j.events({'session': 's'})))

    def test_live_listener_or_mid_turn_session_is_left_to_its_own_delivery(self):
        self.session(kind='waiting', at=time.time(), listening_until=time.time() + 3600, lease_seconds=3600)
        self.session('w', kind='start', at=time.time(), step='Working now')
        for session in ('s', 'w'):
            self.j.set_wake('src', session, True); self.j.send_message('src', session, 'hi')
        self.assertIsNone(self.claim())

    def test_proven_exit_wins_over_a_stale_listener_deadline(self):
        now = time.time()
        self.session(kind='waiting', at=now - 5, listening_until=now + 3600, lease_seconds=3600)
        self.session(kind='stopped', at=now - 1)  # SessionEnd: the listener died with the process
        self.j.set_wake('src', 's', True); self.j.send_message('src', 's', 'hi')
        self.assertEqual(self.claim()['session'], 's')

    def test_archived_sessions_and_recent_input_are_not_resumed(self):
        self.session()
        self.j.set_wake('src', 's', True); self.j.send_message('src', 's', 'hi')
        self.j.archive('src', 's', True)
        self.assertIsNone(self.claim())
        self.j.archive('src', 's', False)
        with patch.object(journal_module, 'RESUME_GRACE', 60):
            self.assertIsNone(self.claim())  # an armed listener gets first chance
        self.assertIsNotNone(self.claim())

    def test_refusal_is_shown_and_held_until_the_user_sends_again(self):
        self.session()
        self.j.set_wake('src', 's', True); self.j.send_message('src', 's', 'hi')
        claimed = self.claim()
        with self.assertRaises(ValueError):
            self.ack(claimed, accepted=True, error='session_open')
        with self.assertRaises(ValueError):
            self.ack(claimed, accepted=False, error='Has /private/path in it\n')
        self.ack(claimed, accepted=False, error='session_open')
        self.assertEqual(self.j.queued(), {('src', 's'): 1})
        self.assertIsNone(self.claim())
        board = self.j.sessions({'session': 's'})[0]
        self.assertEqual(board['resume_hold']['reason'], 'session_open')
        self.j.send_message('src', 's', 'try again')
        self.assertEqual(len(self.claim()['messages']), 2)
        self.assertNotIn('resume_hold', self.j.sessions({'session': 's'})[0])

    def test_runner_presence_is_reported_on_hosts(self):
        self.claim(agents=('claude', 'zelda'))
        host = {h['machine']: h for h in self.j.hosts()}['host-a']
        self.assertEqual(host['runner']['resume_agents'], ['claude'])

    def test_long_poll_releases_when_input_arrives(self):
        self.session(); self.j.set_wake('src', 's', True)
        result = {}
        thread = threading.Thread(target=lambda: result.update(r=self.j.claim_resume('host-a', self.credential, ['claude'], 3)))
        thread.start(); time.sleep(.2)
        self.j.send_message('src', 's', 'from the phone')
        thread.join(3)
        self.assertEqual([m['text'] for m in result['r']['messages']], ['from the phone'])

    def test_http_route_requires_the_host_credential(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.j))
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start(); self.addCleanup(server.shutdown)
        base = 'http://127.0.0.1:%d/hosts/host-a/resume/claim?agents=claude' % server.server_address[1]
        with self.assertRaises(HTTPError) as caught:
            urlopen(Request(base, data=b'', method='POST'), timeout=5)
        self.assertEqual(caught.exception.code, 401)
        self.session(); self.j.set_wake('src', 's', True); self.j.send_message('src', 's', 'hi')
        with urlopen(Request(base, data=b'', method='POST', headers={'Authorization': 'Bearer ' + self.credential}), timeout=5) as response:
            self.assertEqual(json.load(response)['resume']['agent'], 'claude')
        with urlopen(base.split('/hosts')[0] + '/health', timeout=5) as response:
            self.assertEqual(json.load(response)['host_resume'], ['claude', 'codex'])


if __name__ == '__main__':
    unittest.main()
