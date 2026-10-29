"""Read-only browser surface and cursor/filter contracts."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
for project in ('server', 'packages/python/axon_protocol'):
    sys.path.insert(0, str(ROOT/project))
from axon_server.journal import Journal, handler, server_revision


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = Journal(Path(self.tmp.name)/'journal.db')
        self.server = ThreadingHTTPServer(('127.0.0.1',0), handler(self.journal))
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.count = 0

    def stop(self):
        self.server.shutdown();self.server.server_close();self.thread.join()

    def event(self, source='example', session='one', kind='progress', **data):
        self.count += 1
        self.journal.ingest(dict(version=1,source=source,session=session,id=str(self.count),
                                 at=time.time()-20+self.count/1000,kind=kind,data=data))

    def get(self,path):
        with urlopen(self.base+path) as response:return json.load(response)

    def test_activity_time_ignores_heartbeats_and_handles_heartbeat_only_sessions(self):
        self.event(kind='start',agent='claude',goal='Meaningful work')
        first=self.get('/sessions')['sessions'][0]['activity_at']
        self.event(kind='heartbeat',activity='running tools')
        row=self.get('/sessions')['sessions'][0]
        self.assertEqual(row['activity_at'],first)
        self.assertGreater(row['updated_at'],first)
        self.event(step='Real progress')
        newer=self.get('/sessions')['sessions'][0]['activity_at']
        self.assertGreater(newer,first)
        self.event(source='heartbeat-only',kind='heartbeat',agent='pi')
        initial=next(row['activity_at'] for row in self.get('/sessions')['sessions'] if row['source']=='heartbeat-only')
        self.event(source='heartbeat-only',kind='heartbeat',activity='Still alive')
        row=next(row for row in self.get('/sessions')['sessions'] if row['source']=='heartbeat-only')
        self.assertEqual(row['activity_at'],initial)
        self.assertGreater(row['updated_at'],initial)
        self.assertEqual(Journal(self.journal.path).activity_times()[('heartbeat-only','one')],initial)

    def test_version_matches_html_api_and_stream_and_ignores_activity(self):
        with urlopen(self.base+'/') as response:
            revision=response.headers['X-Axon-Version']
            self.assertRegex(revision,r'^[a-f0-9]{64}$')
            self.assertIn(f'data-axon-version="{revision}"',response.read().decode())
        self.event(goal='Activity must not force a page reload')
        for path in ('/health','/sessions','/ui/app.js'):
            with urlopen(self.base+path) as response:self.assertEqual(response.headers['X-Axon-Version'],revision)
        with urlopen(self.base+'/stream',timeout=5) as response:
            self.assertEqual(response.headers['X-Axon-Version'],revision)
        self.assertEqual(server_revision(),revision)

    def test_version_changes_when_shipped_asset_changes(self):
        from importlib.resources import files as actual_files
        original=server_revision()
        class ChangedAsset:
            def __init__(self,parts):self.parts=parts
            def read_bytes(self):
                content=actual_files('axon_server').joinpath(*self.parts).read_bytes()
                return content+b'/* updated */' if self.parts==('web','app.css') else content
        class ChangedRoot:
            def joinpath(self,*parts):return ChangedAsset(parts)
        with patch('axon_server.journal.files',return_value=ChangedRoot()):
            self.assertNotEqual(server_revision(),original)
        self.assertEqual(server_revision(),original)

    def test_packaged_assets_have_security_headers(self):
        for path, kind in [('/', 'text/html'),('/ui/app.js','text/javascript'),('/ui/app.css','text/css'),('/ui/favicon.svg','image/svg+xml')]:
            with urlopen(self.base+path) as response:
                self.assertIn(kind,response.headers['Content-Type'])
                self.assertEqual(response.headers['Cache-Control'],'no-store')
                self.assertEqual(response.headers['X-Content-Type-Options'],'nosniff')
                self.assertIn("frame-ancestors 'none'",response.headers['Content-Security-Policy'])
                self.assertTrue(response.read())
        with urlopen(self.base+'/') as response:
            text=response.read().decode()
            self.assertIn('/ui/app.js',text)
            self.assertNotIn('https://',text)

    def test_host_icons_are_packaged_svg_and_kind_reaches_sessions(self):
        for name in ('laptop','pc-display','hdd-rack','chat-dots'):
            with urlopen(self.base+f'/ui/icons/{name}.svg') as response:
                self.assertEqual(response.headers['Content-Type'],'image/svg+xml')
                self.assertIn(b'<svg',response.read())
        with self.assertRaises(HTTPError) as error:urlopen(self.base+'/ui/icons/LICENSE')
        self.assertEqual(error.exception.code,404);error.exception.close()
        self.event(kind='start',machine='host-a',machine_kind='server')
        self.event(kind='progress',step='No kind from an older reporter')
        self.assertEqual(self.get('/sessions')['sessions'][0]['machine_kind'],'server')

    def post(self,path,body=None,token=None):
        headers={'Content-Type':'application/json',**({'Authorization':'Bearer '+token} if token else {})}
        with urlopen(Request(self.base+path,method='POST',data=json.dumps(body or {}).encode(),headers=headers)) as response:
            return response.status,json.load(response)

    def test_update_requests_need_the_token_and_start_the_updater_once(self):
        root=Path(self.tmp.name)/'deploy';root.mkdir()
        (root/'update-state.json').write_text(json.dumps({'commit':'abc','release':'0.3.0-abc','backup':'/private/path','failed':None}))
        self.stop()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.journal,token='s'*20,updater=root))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'
        status=self.get('/update')
        self.assertEqual((status['commit'],status['requested_at']),('abc',None));self.assertNotIn('backup',status)
        with patch('axon_server.journal.subprocess.run') as start:
            for token in (None,'wrong'*4):
                with self.assertRaises(HTTPError) as error:self.post('/update',token=token)
                self.assertEqual(error.exception.code,401);error.exception.close()
            self.assertFalse((root/'update-requested').exists())
            code,body=self.post('/update',token='s'*20);self.post('/update',token='s'*20)
        self.assertEqual(code,202);self.assertIsNotNone(body['requested_at'])
        self.assertEqual(start.call_count,1);self.assertIn('--no-block',start.call_args[0][0])

    def test_update_endpoint_is_absent_without_an_updater(self):
        with self.assertRaises(HTTPError) as error:urlopen(self.base+'/update')
        self.assertEqual(error.exception.code,404);error.exception.close()
        with self.assertRaises(HTTPError) as error:self.post('/update',token='s'*20)
        self.assertEqual(error.exception.code,501);error.exception.close()

    def test_session_input_requires_token_and_is_claimed_once(self):
        self.event(kind='start',agent='claude')
        with self.assertRaises(HTTPError) as error:self.post('/sessions/example/one/messages',{'text':'hi'},'x'*20)
        self.assertEqual(error.exception.code,403);error.exception.close()
        self.stop()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.journal,token='s'*20))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'
        for token,code in [(None,401),('wrong'*4,401)]:
            with self.assertRaises(HTTPError) as error:self.post('/sessions/example/one/messages',{'text':'hi'},token)
            self.assertEqual(error.exception.code,code);error.exception.close()
        for path,body in [('/sessions/example/missing/messages',{'text':'hi'}),('/sessions/example/one/messages',{'text':' '}),('/sessions/bad%2Fid/one/messages',{'text':'hi'})]:
            with self.assertRaises(HTTPError) as error:self.post(path,body,'s'*20)
            self.assertIn(error.exception.code,(400,404,501));error.exception.close()
        status,message=self.post('/sessions/example/one/messages',{'text':'Ship it'},'s'*20)
        self.assertEqual((status,message['text'],message['delivered_at']),(201,'Ship it',None))
        listing=self.get('/sessions/example/one/messages')
        self.assertTrue(listing['input']);self.assertEqual(len(listing['messages']),1)
        self.assertEqual([m['text'] for m in self.post('/sessions/example/one/inbox/claim')[1]['messages']],['Ship it'])
        self.assertEqual(self.post('/sessions/example/one/inbox/claim')[1]['messages'],[])
        self.assertIsNotNone(self.get('/sessions/example/one/messages')['messages'][0]['delivered_at'])

    def test_board_sessions_carry_queued_input_counts(self):
        self.event(kind='start',agent='claude')
        self.event(session='two',kind='start',agent='claude')
        self.journal.send_message('example','one','First');self.journal.send_message('example','one','Second')
        counts={s['session']:s['queued_messages'] for s in self.get('/sessions')['sessions']}
        self.assertEqual(counts,{'one':2,'two':0})
        self.journal.claim('example','one')
        self.assertEqual(self.get('/sessions?session=one')['sessions'][0]['queued_messages'],0)

    def test_host_claim_authentication_enrollment_revocation_and_wake(self):
        self.event(kind='start', machine='host-a')
        self.stop()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.journal,token='s'*20,allow_legacy_claims=False))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'
        for path, body in [('/hosts/credentials',{'machine':'host-a'}),('/sessions/example/one/wake',{'wake':True})]:
            with self.assertRaises(HTTPError) as error:self.post(path,body)
            self.assertEqual(error.exception.code,401);error.exception.close()
        _,right=self.post('/hosts/credentials',{'machine':'host-a'},'s'*20)
        _,wrong=self.post('/hosts/credentials',{'machine':'host-b'},'s'*20)
        self.journal.send_message('example','one','Keep this queued')
        path='/sessions/example/one/inbox/claim'
        for credential in (None,'invalid',wrong['credential']):
            with self.assertRaises(HTTPError) as error:self.post(path,token=credential)
            self.assertEqual(error.exception.code,401);error.exception.close()
        self.assertFalse(self.get('/sessions')['sessions'][0]['wake'])
        self.post('/sessions/example/one/wake',{'wake':True},'s'*20)
        self.assertTrue(self.get('/sessions')['sessions'][0]['wake'])
        self.assertEqual([m['text'] for m in self.post(path,token=right['credential'])[1]['messages']],['Keep this queued'])
        self.post('/hosts/credentials/revoke',{'credential_id':right['credential_id']},'s'*20)
        with self.assertRaises(HTTPError) as error:self.post(path,token=right['credential'])
        self.assertEqual(error.exception.code,401);error.exception.close()
        self.assertTrue(self.get('/health')['host_claims']['required'])

    def test_legacy_claim_migration_logs_and_still_rejects_invalid_credentials(self):
        self.event(kind='start',machine='host-a')
        path='/sessions/example/one/inbox/claim'
        with self.assertLogs(level='WARNING') as logs:self.post(path)
        self.assertIn('Legacy unauthenticated',logs.output[0])
        with self.assertRaises(HTTPError) as error:self.post(path,token='invalid')
        self.assertEqual(error.exception.code,401);error.exception.close()

    def test_host_sync_status_is_authenticated_bounded_and_visible(self):
        host=self.journal.mint_host_credential('host-a')
        payload={'synced_revision':'a'*40,'ok':True,'error':None}
        for token in (None,'invalid'):
            with self.assertRaises(HTTPError) as error:self.post('/hosts/sync',payload,token)
            self.assertEqual(error.exception.code,401);error.exception.close()
        self.post('/hosts/sync',payload,host['credential'])
        row=self.get('/hosts')['hosts'][0]
        self.assertEqual((row['machine'],row['synced_revision'],row['ok']),('host-a','a'*40,1))
        self.assertFalse(self.journal.hosts('a'*40)[0]['behind'])
        self.assertTrue(self.journal.hosts('b'*40)[0]['behind'])
        for invalid in ({**payload,'error':'/private/path/token'}, {**payload,'machine':'other'}, {**payload,'synced_revision':'not-a-revision'}):
            with self.assertRaises(HTTPError) as error:self.post('/hosts/sync',invalid,host['credential'])
            self.assertEqual(error.exception.code,400);error.exception.close()
        self.post('/hosts/sync',{**payload,'ok':False,'error':'sync_failed'},host['credential'])
        self.assertEqual(self.get('/hosts')['hosts'][0]['error'],'sync_failed')

    def test_revocation_during_long_poll_preserves_pending_input(self):
        self.event(kind='start',machine='host-a')
        host=self.journal.mint_host_credential('host-a')
        result=[]
        def claim():
            try:self.journal.claim('example','one',wait=5,credential=host['credential'])
            except PermissionError:result.append('revoked')
        waiter=threading.Thread(target=claim);waiter.start();time.sleep(.1)
        self.journal.revoke_host_credential(host['credential_id'])
        waiter.join(2)
        self.assertEqual(result,['revoked'])
        self.journal.send_message('example','one','Still here')
        self.assertEqual(self.journal.queued()[('example','one')],1)

    def test_claim_long_poll_wakes_when_input_arrives(self):
        self.event(kind='start',agent='claude')
        result={}
        waiter=threading.Thread(target=lambda:result.update(claim=self.journal.claim('example','one',wait=20)))
        started=time.time();waiter.start();time.sleep(.3)
        self.journal.send_message('example','one','Now')
        waiter.join(5)
        self.assertEqual([m['text'] for m in result['claim']],['Now'])
        self.assertLess(time.time()-started,5)

    def test_stream_signals_each_change(self):
        self.event(kind='start',agent='claude')
        def frame(response):
            lines=[]
            while (line:=response.readline().decode().rstrip('\n')) or not lines:
                if line and not line.startswith(('retry:',':')):lines.append(line)
            return lines
        with urlopen(self.base+'/stream',timeout=5) as response:
            self.assertEqual(response.headers['Content-Type'],'text/event-stream')
            first=frame(response)
            started=time.time()
            self.event(step='Live')
            self.assertNotEqual(frame(response),first)
            self.journal.send_message('example','one','Hi')
            self.assertEqual(frame(response),[f'data: {self.journal.version}'])
            self.assertLess(time.time()-started,5)

    def test_assets_are_allowlisted_and_ui_can_be_disabled(self):
        for path in ('/ui/../journal.py','/ui/%2e%2e/journal.py','/ui/secrets.json'):
            with self.assertRaises(HTTPError) as error:urlopen(self.base+path)
            self.assertEqual(error.exception.code,404);error.exception.close()
        self.stop()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.journal,web=False))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'
        with self.assertRaises(HTTPError) as error:urlopen(self.base+'/')
        self.assertEqual(error.exception.code,404);error.exception.close()
        self.assertTrue(self.get('/health')['ok'])

    def test_newest_first_cursor_does_not_duplicate_or_skip_events(self):
        for i in range(125):self.event(goal=f'Update {i}')
        seen=[];before=None
        while True:
            params={'order':'desc','limit':50}
            if before:params['before']=before
            page=self.get('/events?'+urlencode(params))
            seen.extend(event['seq'] for event in page['events'])
            if len(page['events'])<50:break
            before=page['next_before']
        self.assertEqual(seen,list(range(125,0,-1)))
        self.assertEqual(self.get('/events?limit=2')['next_after'],2)
        self.assertEqual([e['seq'] for e in self.get('/events?after=123')['events']],[124,125])
        with self.assertRaises(HTTPError) as error:self.get('/events?order=invalid')
        self.assertEqual(error.exception.code,400);error.exception.close()

    def test_timeline_filters_match_sessions_and_literal_search(self):
        self.event(kind='start',project='glass',agent='codex',machine='host-a',goal='Investigate 50%_done')
        self.event(kind='heartbeat',activity='reading')
        self.event(source='other',kind='start',project='gabbie',agent='claude',machine='host-b',goal='Different work')
        params=urlencode({'project':'glass','agent':'codex','machine':'host-a','exclude_kind':'heartbeat','q':'50%_'})
        result=self.get('/events?'+params)['events']
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['data']['project'],'glass')
        self.assertEqual(self.get('/events?'+urlencode({'q':"' OR 1=1 --"}))['events'],[])
        self.assertEqual(len(self.get('/sessions?machine=host-a')['sessions']),1)
        self.assertEqual(self.get('/events?machine=missing')['events'],[])
        future=str(time.time()+1)
        self.assertEqual(self.get('/events?since='+future)['events'],[])

    def test_text_remains_json_data_and_post_is_not_a_write_api(self):
        text='<img src=x onerror=alert(1)>'
        self.event(kind='start',project=text,goal=text)
        self.assertEqual(self.get('/sessions')['sessions'][0]['goal'],text)
        request=Request(self.base+'/events',method='POST',data=b'{}')
        with self.assertRaises(HTTPError) as error:urlopen(request)
        self.assertEqual(error.exception.code,501);error.exception.close()
        self.assertEqual(len(self.journal.events()),1)


if __name__=='__main__':unittest.main()
