import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request,urlopen

ROOT=Path(__file__).resolve().parents[2]
for package in ('server','packages/python/axon_protocol'):
    sys.path.insert(0,str(ROOT/package))
from axon_server.journal import Journal,handler


class WakeInboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.j=Journal(Path(self.tmp.name)/'journal.db')
        self.j.ingest(dict(version=1,source='test',session='s',id='start',at=time.time(),kind='start',
                           data={'agent':'pi','machine':'host-a','goal':'Keep semantic work','step':'Keep this step'}))
        self.credential=self.j.mint_host_credential('host-a')['credential']

    def claim(self):return self.j.claim('test','s',credential=self.credential,wake_path='pi-native')

    def ack(self,messages,accepted=True):
        return self.j.acknowledge_wake('test','s',self.credential,
            {'ids':[m['id'] for m in messages],'claim_token':messages[0]['claim_token'],'accepted':accepted})

    def test_consent_and_matching_agent_are_required_without_consuming_input(self):
        self.j.send_message('test','s','Private dashboard input')
        self.assertEqual(self.claim(),[])
        self.j.set_wake('test','s',True)
        self.assertEqual(self.j.claim('test','s',credential=self.credential,wake_path='codex-queue'),[])
        with self.assertRaises(PermissionError):self.j.claim('test','s',wake_path='pi-native')
        self.assertEqual(len(self.claim()),1)

    def test_leases_acknowledgment_audit_and_duplicate_ack(self):
        self.j.set_wake('test','s',True);self.j.send_message('test','s','PRIVATE TEXT')
        batch=self.claim();self.assertEqual(self.claim(),[])
        self.assertEqual(self.j.claim('test','s',credential=self.credential),[])
        self.assertEqual(self.j.queued()[('test','s')],1)
        self.assertIsNone(self.j.messages('test','s')[0]['delivered_at'])
        self.assertNotIn('claim_token',self.j.messages('test','s')[0])
        self.ack(batch);self.ack(batch)
        self.assertIsNotNone(self.j.messages('test','s')[0]['delivered_at'])
        events=self.j.events();self.assertEqual(len(events),2)
        self.assertNotIn('PRIVATE TEXT',json.dumps(events))
        self.assertIn('input token via pi-native',events[-1]['data']['activity'])
        session=self.j.sessions()[0]
        self.assertEqual((session['goal'],session['step']),('Keep semantic work','Keep this step'))
        self.assertIn('last_wake',session)

    def test_failed_or_expired_claim_requeues_and_revocation_blocks_ack(self):
        self.j.set_wake('test','s',True);self.j.send_message('test','s','Retry')
        first=self.claim();self.ack(first,False)
        second=self.claim();self.assertNotEqual(first[0]['claim_token'],second[0]['claim_token'])
        with self.j.connect() as db:db.execute('UPDATE message_claims SET until_at=0')
        with self.assertRaises(ValueError):self.ack(second)
        third=self.claim();self.assertEqual(len(third),1)
        self.j.revoke_host_credential(__import__('hashlib').sha256(self.credential.encode()).hexdigest())
        with self.assertRaises(PermissionError):self.ack(third)
        self.assertEqual(self.j.queued()[('test','s')],1)

    def server(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.j))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(thread.join);self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        return server

    def test_http_default_denies_unauthenticated_claims(self):
        server=self.server()
        for path in ('inbox/claim','inbox/wake/claim?path=pi-native','inbox/wake/ack'):
            request=Request(f'http://127.0.0.1:{server.server_port}/sessions/test/s/'+path,method='POST',data=b'')
            with self.assertRaises(HTTPError) as error:urlopen(request)
            self.assertEqual(error.exception.code,401);error.exception.close()

    def test_old_long_poll_cannot_bypass_consent(self):
        server=self.server();self.j.send_message('test','s','Keep queued')
        request=Request(f'http://127.0.0.1:{server.server_port}/sessions/test/s/inbox/claim?wait=50',
            method='POST',data=b'',headers={'Authorization':'Bearer '+self.credential})
        with self.assertRaises(HTTPError) as error:urlopen(request)
        self.assertEqual(error.exception.code,400);error.exception.close()
        self.assertIsNone(self.j.messages('test','s')[0]['delivered_at'])

    def test_message_storage_stays_compatible_with_old_server_inserts(self):
        with self.j.connect() as db:
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?)',('old','test','s',time.time(),'Previous release',None))
        self.assertEqual(self.j.messages('test','s')[0]['text'],'Previous release')

    def test_closing_a_long_poll_cancels_its_server_request(self):
        self.j.set_wake('test','s',True);server=self.server()
        entered,exited=threading.Event(),threading.Event()
        original=self.j.claim
        def tracked(*args,**kwargs):
            entered.set()
            try:return original(*args,**kwargs)
            finally:exited.set()
        with patch.object(self.j,'claim',side_effect=tracked):
            connection=socket.create_connection(('127.0.0.1',server.server_port))
            connection.sendall(('POST /sessions/test/s/inbox/wake/claim?wait=50&path=pi-native HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\nAuthorization: Bearer '+self.credential+'\r\n\r\n').encode())
            self.assertTrue(entered.wait(2));connection.close()
            self.assertTrue(exited.wait(2),'Disconnected native plugin left a long-poll behind')
