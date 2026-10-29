import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
for package in ('server', 'packages/python/axon_protocol'):
    sys.path.insert(0, str(ROOT/package))
from axon_server.journal import Journal, handler

TOKEN='test-input-token'


class HostJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.journal=Journal(Path(self.tmp.name)/'journal.db')
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.journal,token=TOKEN))
        self.addCleanup(self.server.server_close)
        thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        thread.start();self.addCleanup(self.server.shutdown)
        self.base='http://127.0.0.1:%d' % self.server.server_address[1]
        self.credential=self.journal.mint_host_credential('host-a')['credential']

    def request(self,path,payload=None,auth=None,expect=200):
        headers={'Authorization':'Bearer '+auth} if auth else {}
        data=json.dumps(payload).encode() if payload is not None else b''
        try:
            with urlopen(Request(self.base+path,data=data,method='POST',headers=headers),timeout=5) as response:
                body=json.load(response) if response.length!=0 else {}
                self.assertEqual(response.status,expect)
                return body
        except HTTPError as error:
            self.assertEqual(error.code,expect)
            return {'error':error.code}

    def get(self,path,expect=200):
        try:
            with urlopen(self.base+path,timeout=5) as response:
                self.assertEqual(response.status,expect)
                return json.load(response)
        except HTTPError as error:
            self.assertEqual(error.code,expect)
            return {'error':error.code}

    def post_job(self,**overrides):
        job={'agent':'claude','project':'sample','prompt':'Do the thing'}
        return self.request('/hosts/host-a/jobs',{**job,**overrides},auth=TOKEN,expect=201)['job']

    def test_enqueue_needs_input_token_and_only_accepts_bare_names(self):
        self.request('/hosts/host-a/jobs',{'agent':'claude','project':'sample','prompt':'x'},expect=401)
        self.request('/hosts/host-a/jobs',{'agent':'zelda','project':'sample','prompt':'x'},auth=TOKEN,expect=400)
        self.request('/hosts/host-a/jobs',{'agent':'claude','project':'/home/private/repo','prompt':'x'},auth=TOKEN,expect=400)
        self.request('/hosts/host-a/jobs',{'agent':'claude','project':'sample','prompt':'  '},auth=TOKEN,expect=400)
        job=self.post_job()
        self.assertEqual((job['status'],job['worktree']),('queued',True))

    def test_claim_requires_host_credential_for_the_addressed_machine(self):
        job=self.post_job()
        self.request('/hosts/host-a/jobs/claim',expect=401)
        self.request('/hosts/host-a/jobs/claim',auth=self.credential+'wrong',expect=401)
        other=self.journal.mint_host_credential('host-b')['credential']
        self.request('/hosts/host-a/jobs/claim',auth=other,expect=401)
        claimed=self.request('/hosts/host-a/jobs/claim',auth=self.credential)['job']
        self.assertEqual((claimed['id'],claimed['status']),(job['id'],'claimed'))
        self.assertIsNone(self.request('/hosts/host-a/jobs/claim',auth=self.credential)['job'])

    def test_claims_are_fifo_and_scoped_to_the_machine(self):
        first=self.post_job(prompt='first');second=self.post_job(prompt='second')
        self.post_job(agent='codex')
        order=[self.request('/hosts/host-a/jobs/claim',auth=self.credential)['job']['id'],
               self.request('/hosts/host-a/jobs/claim',auth=self.credential)['job']['id'],
               self.request('/hosts/host-a/jobs/claim',auth=self.credential)['job']['id']]
        self.assertEqual(order[:2],[first['id'],second['id']])
        self.assertEqual(self.get('/hosts/host-b/jobs')['jobs'],[])

    def test_reports_follow_lifecycle_and_terminal_states_stick(self):
        job=self.post_job()
        self.request('/hosts/host-a/jobs/%s/report'%job['id'],{'status':'started'},auth=self.credential,expect=400)
        claimed_id=self.request('/hosts/host-a/jobs/claim',auth=self.credential)['job']['id']
        self.assertEqual(claimed_id,job['id'])
        self.request('/hosts/host-a/jobs/%s/report'%job['id'],{'status':'started','detail':'launched as axon-x'},auth=self.credential)
        self.request('/hosts/host-a/jobs/%s/report'%job['id'],{'status':'refused'},auth=self.credential,expect=400)
        self.request('/hosts/host-a/jobs/%s/report'%job['id'],{'status':'finished'},auth=self.credential)
        self.request('/hosts/host-a/jobs/%s/report'%job['id'],{'status':'failed'},auth=self.credential,expect=400)
        self.request('/hosts/host-a/jobs/nope/report',{'status':'started'},auth=self.credential,expect=404)
        listed=self.get('/hosts/host-a/jobs')['jobs']
        self.assertEqual([(j['status'],j['detail']) for j in listed],[('finished','launched as axon-x')])

    def test_long_poll_claim_releases_when_a_job_arrives(self):
        started=time.time()
        self.assertIsNone(self.request('/hosts/host-a/jobs/claim?wait=1',auth=self.credential)['job'])
        self.assertGreaterEqual(time.time()-started,0.9)
        def enqueue():
            time.sleep(.2)
            self.post_job(prompt='wakes the runner')
        thread=threading.Thread(target=enqueue);thread.start();self.addCleanup(thread.join)
        claimed=self.request('/hosts/host-a/jobs/claim?wait=5',auth=self.credential)['job']
        thread.join()
        self.assertEqual(claimed['prompt'],'wakes the runner')

    def test_retention_drops_finished_jobs_only(self):
        done=self.post_job();kept=self.post_job()
        self.request('/hosts/host-a/jobs/claim',auth=self.credential)  # FIFO hands out `done`
        self.request('/hosts/host-a/jobs/%s/report'%done['id'],{'status':'refused','detail':'not_allowed'},auth=self.credential)
        for job,ended in ((done,'ended_at'),(kept,None)):
            with self.journal.connect() as db:
                if ended:
                    db.execute('UPDATE host_jobs SET ended_at=? WHERE id=?',(time.time()-40*86400,job['id']))
                db.execute('UPDATE host_jobs SET at=? WHERE id=?',(time.time()-40*86400,job['id']))
        self.journal.prune()
        self.assertEqual([j['id'] for j in self.journal.jobs('host-a')],[kept['id']])


if __name__=='__main__':
    unittest.main()
