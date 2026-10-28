"""Persistent board archiving, independent of reported work state."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ROOT=Path(__file__).resolve().parents[2]
for project in ('server','packages/python/axon_protocol'):
    sys.path.insert(0,str(ROOT/project))
from axon_server.journal import Journal,handler

class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'journal.db';self.journal=Journal(self.path);self.n=0
        self.event('start',at=time.time()-30,goal='Keep history',project='example')
    def event(self,kind='heartbeat',at=None,**data):
        self.n+=1
        event=dict(version=1,source='agent',session='chat',id=str(self.n),at=at or time.time(),kind=kind,data=data)
        self.journal.ingest(event);return event
    def test_archive_survives_restart_and_background_reports_without_changing_work(self):
        before=self.journal.sessions()[0];version=self.journal.version
        self.journal.archive('agent','chat',True)
        self.assertGreater(self.journal.version,version)
        for kind in ('heartbeat','progress','waiting'):
            self.event(kind,step='Background update')
        journal=Journal(self.path);row=journal.sessions()[0]
        self.assertIn('archived_at',row)
        self.assertEqual(row['goal'],before['goal'])
        self.assertEqual(row['state'],'waiting')
        self.assertEqual(len(journal.events()),4)
        journal.archive('agent','chat',False)
        self.assertNotIn('archived_at',Journal(self.path).sessions()[0])
        self.assertEqual(len(journal.events()),4)
    def test_only_fresh_user_turn_restores(self):
        old_turn=self.event('progress',at=time.time()-20,user_turn=True,state='working')
        self.journal.archive('agent','chat',True)
        self.journal.ingest(old_turn) # replay cannot undo the user's choice
        self.event('progress',at=time.time()-10,user_turn=True)
        self.assertIn('archived_at',self.journal.sessions()[0])
        self.event('start',goal='Agent-generated task')
        self.assertIn('archived_at',self.journal.sessions()[0])
        self.event('progress',user_turn=True,state='working')
        self.assertNotIn('archived_at',self.journal.sessions()[0])
        self.assertNotIn('user_turn',self.journal.sessions()[0])
    def test_repeated_archive_is_idempotent_and_validated(self):
        self.journal.archive('agent','chat',True);at=self.journal.sessions()[0]['archived_at']
        self.journal.archive('agent','chat',True)
        self.assertEqual(self.journal.sessions()[0]['archived_at'],at)
        with self.assertRaises(ValueError):self.journal.archive('agent','chat','false')
        with self.assertRaises(LookupError):self.journal.archive('missing','chat',True)
        with self.assertRaises(ValueError):self.event('progress',user_turn=1)
    def test_http_archive_requires_same_token_as_messages(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.journal,token='test-archive-token'))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            base=f'http://127.0.0.1:{server.server_port}'
            def post(payload,token=None,path='/sessions/agent/chat/archive'):
                headers={'Content-Type':'application/json'}
                if token:headers['Authorization']='Bearer '+token
                return urlopen(Request(base+path,data=json.dumps(payload).encode(),headers=headers,method='POST'))
            for token in (None,'wrong'):
                with self.assertRaises(HTTPError) as error:post({'archived':True},token)
                self.assertEqual(error.exception.code,401);error.exception.close()
            for payload in ({},{'archived':'false'},{'archived':1}):
                with self.assertRaises(HTTPError) as error:post(payload,'test-archive-token')
                self.assertEqual(error.exception.code,400);error.exception.close()
            with post({'archived':True},'test-archive-token') as response:
                self.assertTrue(json.load(response)['archived'])
            with urlopen(base+'/sessions') as response:self.assertIn('archived_at',json.load(response)['sessions'][0])
            with post({'archived':False},'test-archive-token') as response:
                self.assertFalse(json.load(response)['archived'])
            with self.assertRaises(HTTPError) as error:post({'archived':True},'test-archive-token','/sessions/unknown/chat/archive')
            self.assertEqual(error.exception.code,404);error.exception.close()
        finally:server.shutdown();server.server_close();thread.join()
    def test_retention_removes_archive_metadata_with_expired_session(self):
        self.journal.archive('agent','chat',True)
        self.journal.prune(now=time.time()+31*86400)
        with self.journal.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM session_archives').fetchone()[0],0)
