"""Resource snapshots are account-level, whitelisted, durable and separate from work."""
import copy
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parents[2]
for path in ('packages/python/axon_protocol','packages/python/axon_reporter','server'):
    sys.path.insert(0,str(ROOT/path))
from axon_protocol.resources import validate_resource
from axon_reporter import resources
from axon_server.journal import Journal,handler

class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.env=patch.dict(os.environ,AXON_ACTIVITY_HOME=str(self.root/'state'),AXON_CODEX_ACCOUNT='primary',AXON_CLAUDE_ACCOUNT='primary',AXON_MOSQUITTO_PUB='/missing/axon-pub')
        self.env.start();self.addCleanup(self.env.stop)
        self.now=time.time()
    def sample(self,used=25,now=None):
        return resources.codex_snapshot({'rateLimits':{'primary':{'usedPercent':used,'windowDurationMins':300,'resetsAt':self.now+3600},'secondary':{'usedPercent':0,'windowDurationMins':10080,'resetsAt':self.now+86400}}},self.now if now is None else now)
    def test_normalizes_documented_fields_without_copying_account_credentials(self):
        raw={'rateLimits':{'primary':{'usedPercent':0,'windowDurationMins':300,'resetsAt':self.now+1}},'credentials':'private-sentinel','accountId':'private-sentinel'}
        value=resources.codex_snapshot(raw,self.now)
        self.assertEqual(value['windows'][0]['used_percent'],0)
        self.assertNotIn('private-sentinel',json.dumps(value))
        claude=resources.claude_snapshot({'rate_limits':{'five_hour':{'used_percentage':22.5,'resets_at':self.now+300}},'session_id':'private-sentinel'},self.now)
        self.assertEqual(claude['windows'][0]['used_percent'],22.5)
        self.assertNotIn('private-sentinel',json.dumps(claude))
        self.assertIsNone(resources.claude_snapshot({}))
        self.assertEqual(resources.claude_snapshot({'rate_limits':{'seven_day':{'used_percentage':None,'resets_at':None}}},self.now)['windows'][0]['used_percent'],None)
    def test_rejects_invalid_percentages_unknown_fields_and_topic_names(self):
        for value in (-1,101,True,float('nan'),float('inf'),'50'):
            sample=self.sample();sample['windows'][0]['used_percent']=value
            with self.assertRaises(ValueError):validate_resource(sample)
        for key,value in [('account','bad/topic'),('provider',[]),('version',True),('observed_at',self.now+1000)]:
            sample=self.sample();sample[key]=value
            with self.assertRaises(ValueError):validate_resource(sample)
        # A field this version does not know is dropped, never stored or forwarded (#22).
        sample=self.sample();sample['credential']='private'
        self.assertNotIn('credential',validate_resource(sample))
        sample=self.sample();sample['credential']='x'*4000
        with self.assertRaises(ValueError):validate_resource(sample)
        sample=self.sample();sample['windows'].append(copy.deepcopy(sample['windows'][0]))
        with self.assertRaises(ValueError):validate_resource(sample)
    def test_queue_throttles_identical_status_callbacks_and_keeps_changed_usage(self):
        self.now-=120
        with patch.object(resources.report,'drain'):
            self.assertTrue(resources.publish(self.sample()))
            self.assertFalse(resources.publish(self.sample(now=self.now+10)))
            self.assertTrue(resources.publish(self.sample(used=30,now=self.now+20)))
            self.assertTrue(resources.publish(self.sample(used=30,now=self.now+81)))
        with closing(sqlite3.connect(self.root/'state/outbox.sqlite3')) as db:
            rows=db.execute('SELECT topic,payload FROM outbox').fetchall()
        self.assertEqual(len(rows),3);self.assertEqual(rows[0][0],'axon/resources/codex/primary')
        self.assertEqual((self.root/'state/outbox.sqlite3').stat().st_mode & 0o777,0o600)
    def test_server_keeps_latest_account_snapshot_across_restarts_without_sessions(self):
        journal=Journal(self.root/'journal.db');self.assertEqual(len(journal.resources()),2)
        before=journal.version
        self.assertTrue(journal.ingest_resource(self.sample()))
        self.assertGreater(journal.version,before)
        self.assertFalse(journal.ingest_resource(self.sample(used=99,now=self.now-1)))
        self.assertFalse(journal.ingest_resource(self.sample()))
        self.assertTrue(journal.ingest_resource(self.sample(used=30,now=self.now+1)))
        restored=Journal(journal.path);value=next(r for r in restored.resources() if r['provider']=='codex')
        self.assertEqual(value['windows'][0]['used_percent'],30)
        self.assertEqual(restored.sessions(),[]);self.assertEqual(restored.events(),[])
    def test_resource_endpoint_has_both_standing_providers_and_updates(self):
        journal=Journal(self.root/'journal.db');server=ThreadingHTTPServer(('127.0.0.1',0),handler(journal));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            base=f'http://127.0.0.1:{server.server_port}'
            with urlopen(base+'/resources') as response:
                data=json.load(response)['resources'];self.assertEqual({r['provider'] for r in data},{'codex','claude'})
                self.assertTrue(all(r['observed_at'] is None for r in data))
            journal.ingest_resource(self.sample())
            with urlopen(base+'/resources') as response:
                data=json.load(response)['resources'];self.assertEqual(next(r for r in data if r['provider']=='codex')['windows'][0]['used_percent'],25)
        finally:server.shutdown();server.server_close();thread.join()
    def test_codex_collector_uses_native_rpc_without_starting_an_inference_turn(self):
        fake=self.root/'fake-codex';log=self.root/'methods'
        fake.write_text('#!'+sys.executable+'\nimport json,sys\nfrom pathlib import Path\nfor line in sys.stdin:\n r=json.loads(line);p=Path('+repr(str(log))+');p.write_text((p.read_text() if p.exists() else "")+r["method"]+"\\n")\n if "id" in r: print(json.dumps({"id":r["id"],"result":{} if r["method"]=="initialize" else {"rateLimits":{"primary":{"usedPercent":10,"windowDurationMins":300,"resetsAt":None}}}}),flush=True)\n')
        fake.chmod(0o700)
        result=resources.read_codex(str(fake));self.assertEqual(result['windows'][0]['used_percent'],10)
        self.assertEqual(log.read_text().splitlines(),['initialize','initialized','account/rateLimits/read'])
    def test_claude_statusline_preserves_existing_output_and_queues_only_quotas(self):
        raw={'rate_limits':{'five_hour':{'used_percentage':15,'resets_at':self.now+3600}},'unrelated':'private-sentinel'}
        env={**os.environ,'PYTHONPATH':os.pathsep.join(str(ROOT/p) for p in ('packages/python/axon_protocol','packages/python/axon_reporter'))}
        result=subprocess.run([sys.executable,'-m','axon_reporter.resources','claude-statusline','--display-command','printf "existing status"'],input=json.dumps(raw),text=True,capture_output=True,check=True,env=env)
        self.assertEqual(result.stdout,'existing status');self.assertEqual(result.stderr,'')
        with closing(sqlite3.connect(self.root/'state/outbox.sqlite3')) as db:payload=db.execute('SELECT payload FROM outbox').fetchone()[0]
        self.assertNotIn('private-sentinel',payload)
