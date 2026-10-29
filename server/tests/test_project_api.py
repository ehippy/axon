import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request,urlopen

ROOT=Path(__file__).resolve().parents[2]
for package in ('server','packages/python/axon_protocol'):
    sys.path.insert(0,str(ROOT/package))
from axon_server.journal import Journal,handler


class ProjectAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.journal=Journal(Path(self.tmp.name)/'journal.db')
        self.seq=0

    def event(self,session='s',project_id='a'*64,machine='host-a',project='sample',**extra):
        self.seq+=1
        data={'agent':'codex','machine':machine,'project':project,**extra}
        if project_id:data['project_id']=project_id
        self.journal.ingest(dict(version=1,source='test',session=session,id=str(self.seq),
            at=time.time(),kind='progress',data=data))

    def test_worktree_host_inventory_grouping_token_totals_and_legacy_fallback(self):
        self.event(tokens={'input':100,'cached':20,'output':10})
        self.event(session='tree',machine='host-b',project='different-clone-name',worktree='parallel',branch='feature',
                   tokens={'input':50,'cached':0,'output':5})
        self.event(session='other',project_id='b'*64) # same name, different repository
        self.event(session='old',project_id=None,project='legacy')
        groups={p['id']:p for p in self.journal.projects(since=0)}
        self.assertEqual(set(groups),{'a'*64,'b'*64,'legacy:legacy'})
        shared=groups['a'*64]
        self.assertEqual(len(shared['sessions']),2)
        self.assertEqual({s['project'] for s in shared['sessions']},{'sample'})
        self.assertEqual({h['machine'] for h in shared['hosts']},{'host-a','host-b'})
        self.assertEqual(shared['tokens'],{'input':150,'cached':20,'output':15,'sessions':2})
        self.assertEqual(next(h for h in shared['hosts'] if h['machine']=='host-b')['checkouts'],
                         [{'branch':'feature','worktree':'parallel'}])
        self.assertGreater(shared['last_activity'],0)

    def test_overrides_merge_and_hide_without_rewriting_history(self):
        self.event(project='fork')
        self.event(session='upstream',project_id='b'*64,project='upstream')
        before=self.journal.events()
        self.journal.update_project('b'*64,{'name':'Together','hidden':True})
        self.journal.update_project('a'*64,{'merge_into':'b'*64})
        group,=self.journal.projects()
        self.assertEqual((group['id'],group['name'],group['hidden']),('b'*64,'Together',True))
        self.assertEqual(len(group['sessions']),2)
        self.assertTrue(all(s['project_hidden'] and s['project']=='Together' and s['project_id']=='b'*64 for s in group['sessions']))
        self.assertEqual(len(self.journal.sessions({'project':'b'*64})),2)
        self.assertEqual(len(self.journal.events({'project':'b'*64})),2)
        self.assertEqual(self.journal.events(),before)
        with self.assertRaises(ValueError):self.journal.update_project('b'*64,{'merge_into':'b'*64})
        again=Journal(self.journal.path)
        self.assertEqual(again.projects()[0]['name'],'Together')
        self.assertEqual(len(again.projects()[0]['sessions']),2)

    def test_project_http_overrides_require_input_token(self):
        self.event()
        server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.journal,token='t'*20))
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        self.addCleanup(worker.join);self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        base=f'http://127.0.0.1:{server.server_port}'
        path='/projects/'+'a'*64
        for token in (None,'invalid'):
            request=Request(base+path,method='POST',data=b'{"name":"Renamed"}',headers={'Authorization':'Bearer '+(token or '')})
            with self.assertRaises(HTTPError) as error:urlopen(request)
            self.assertEqual(error.exception.code,401);error.exception.close()
        request=Request(base+path,method='POST',data=b'{"name":"Renamed"}',headers={'Authorization':'Bearer '+'t'*20})
        with urlopen(request) as response:self.assertTrue(json.load(response)['updated'])
        with urlopen(base+path) as response:self.assertEqual(json.load(response)['name'],'Renamed')
        with urlopen(base+'/projects') as response:self.assertEqual(len(json.load(response)['projects']),1)
        with self.assertRaises(HTTPError) as error:urlopen(base+'/projects?since=nan')
        self.assertEqual(error.exception.code,400);error.exception.close()
