import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer

ROOT=Path(__file__).resolve().parents[2]
for package in ('server','packages/python/axon_protocol','packages/python/axon_reporter'):
    sys.path.insert(0,str(ROOT/package))
from axon_server.journal import Journal,handler
from axon_reporter import native_inbox,report


class NativeInboxBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.j=Journal(self.root/'journal.db');self.children=[]
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.j));self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        credential=self.root/'credential';credential.write_text(self.j.mint_host_credential('host-a')['credential']);credential.chmod(0o600)
        self.env={**os.environ,'AXON_CONFIG_FILE':str(self.root/'absent'),'AXON_HOST_CREDENTIAL_FILE':str(credential),
            'AXON_ACTIVITY_HOME':str(self.root/'state'),'AXON_MACHINE':'host-a','AXON_SOURCE':'pi-test',
            'AXON_WAKE':'pi','AXON_SERVER':f'http://127.0.0.1:{self.server.server_port}'}
        with patch.dict(os.environ,self.env):self.source,self.key,_=report.identity('pi','private-native-id')
        self.j.ingest(dict(version=1,source=self.source,session=self.key,id='start',at=time.time(),kind='start',data={'agent':'pi','machine':'host-a'}))
        self.j.set_wake(self.source,self.key,True)
        self.addCleanup(self.clean)

    def clean(self):
        for child in self.children:
            if child.poll() is None:child.terminate()
            child.wait(timeout=5)
            for stream in (child.stdin,child.stdout,child.stderr):stream.close()
        self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()

    def start(self,**env):
        child=subprocess.Popen([sys.executable,str(Path(native_inbox.__file__)),'--agent','pi'],
            env={**self.env,**env},stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.children.append(child)
        child.stdin.write(json.dumps({'session_id':'private-native-id'})+'\n');child.stdin.flush()
        return child

    def wait_delivered(self):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            if self.j.messages(self.source,self.key)[0]['delivered_at'] is not None:return
            time.sleep(.02)
        self.fail('Native acknowledgment did not reach the server')

    def test_message_is_not_delivered_until_native_api_accepts(self):
        self.j.send_message(self.source,self.key,'Private dashboard instruction')
        child=self.start()
        self.assertTrue(select.select([child.stdout],[],[],5)[0])
        batch=json.loads(child.stdout.readline())['messages']
        self.assertEqual([m['text'] for m in batch],['Private dashboard instruction'])
        self.assertIsNone(self.j.messages(self.source,self.key)[0]['delivered_at'])
        child.stdin.write('{"accepted":true}\n');child.stdin.flush()
        self.wait_delivered()
        events=json.dumps(self.j.events())
        self.assertNotIn('Private dashboard instruction',events);self.assertNotIn('private-native-id',events)
        self.assertIn('pi-native',events)

    def test_retry_of_a_locally_accepted_message_does_not_reinject(self):
        message=self.j.send_message(self.source,self.key,'Already accepted by the native API')
        with patch.dict(os.environ,self.env):
            native_inbox.accepted_ids('pi','private-native-id',[message['id']],remember=True)
            self.assertEqual(report.spool_path().stat().st_mode&0o777,0o600)
        child=self.start();self.wait_delivered()
        self.assertFalse(select.select([child.stdout],[],[],.1)[0],'Retried acknowledgment emitted a duplicate native message')

    def test_host_opt_in_is_required(self):
        self.j.send_message(self.source,self.key,'Keep queued')
        child=self.start(AXON_WAKE='')
        self.assertEqual(child.wait(timeout=5),0)
        self.assertEqual(child.stdout.read(),'')
        self.assertIsNone(self.j.messages(self.source,self.key)[0]['delivered_at'])

    def test_an_agent_api_failure_releases_its_lease(self):
        self.j.send_message(self.source,self.key,'Retry safely')
        child=self.start()
        self.assertTrue(select.select([child.stdout],[],[],5)[0]);json.loads(child.stdout.readline())
        child.stdin.write('{"accepted":false}\n');child.stdin.flush()
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            with self.j.connect() as db:leased=db.execute('SELECT COUNT(*) FROM message_claims').fetchone()[0]
            if not leased:break
            time.sleep(.02)
        self.assertEqual(leased,0)
        self.assertIsNone(self.j.messages(self.source,self.key)[0]['delivered_at'])
