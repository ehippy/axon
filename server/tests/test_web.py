"""Read-only browser surface and cursor/filter contracts."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
for project in ('server', 'packages/python/axon_protocol'):
    sys.path.insert(0, str(ROOT/project))
from axon_server.journal import Journal, handler


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

    def test_claim_long_poll_wakes_when_input_arrives(self):
        self.event(kind='start',agent='claude')
        result={}
        waiter=threading.Thread(target=lambda:result.update(claim=self.journal.claim('example','one',wait=20)))
        started=time.time();waiter.start();time.sleep(.3)
        self.journal.send_message('example','one','Now')
        waiter.join(5)
        self.assertEqual([m['text'] for m in result['claim']],['Now'])
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
