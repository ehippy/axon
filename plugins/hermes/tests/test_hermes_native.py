import importlib.util
import io
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[3]
for package in ('axon_reporter','axon_protocol'):
    sys.path.insert(0,str(ROOT/'packages/python'/package))


class Pipe(io.StringIO):
    def close(self):pass


class Lines:
    def __init__(self):self.queue=queue.Queue();self.closed=False
    def __iter__(self):
        while (line:=self.queue.get()) is not None:yield line
    def close(self):self.closed=True


class Child:
    def __init__(self):self.stdin=Pipe();self.stdout=Lines();self.terminated=False
    def terminate(self):self.terminated=True;self.stdout.queue.put(None)
    def kill(self):self.terminate()
    def wait(self,timeout=None):return 0
    def push(self):self.stdout.queue.put(json.dumps({'messages':[{'id':'message','at':time.time(),'text':'Dashboard input'}]})+'\n')


class Context:
    def __init__(self,surface='cli',allowed=False,accepted=True,gateway_backed=False):
        self.surface=surface;self.allowed=allowed;self.accepted=accepted
        self.hooks={};self.calls=[];self.received=threading.Event()
        self._manager=SimpleNamespace(_cli_ref=object() if surface=='cli' and not gateway_backed else None)
    def register_hook(self,name,callback):self.hooks[name]=callback
    def on_unload(self,callback):self.unload=callback
    def session_surface(self):return self.surface
    def session_cwd(self):return '/tmp/example-project'
    def _gateway_injection_allowed(self):return self.allowed
    def inject_message(self,text,**kwargs):
        self.calls.append((text,kwargs));self.received.set();return self.accepted


class HermesNativeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        env=patch.dict(os.environ,{'AXON_WAKE':'hermes','AXON_ACTIVITY_HOME':self.tmp.name,'AXON_CONFIG_FILE':str(Path(self.tmp.name)/'absent')})
        env.start();self.addCleanup(env.stop)
        spec=importlib.util.spec_from_file_location('hermes_native_test',ROOT/'plugins/hermes/__init__.py')
        self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)
        self.children=[]
        def spawn(*args,**kwargs):
            child=Child();self.children.append(child);return child
        for target in ('lifecycle','claim'):
            patched=patch.object(self.module,target,return_value=[]);patched.start();self.addCleanup(patched.stop)
        self.emitted=patch.object(self.module.report,'report');self.emit=self.emitted.start();self.addCleanup(self.emitted.stop)
        patched=patch.object(self.module.subprocess,'Popen',side_effect=spawn);self.spawn=patched.start();self.addCleanup(patched.stop)

    def setup(self,**kwargs):
        ctx=Context(**kwargs);self.module.register(ctx)
        self.addCleanup(ctx.unload)
        return ctx

    def wait_ack(self,child):
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            lines=child.stdin.getvalue().splitlines()
            if len(lines)>1:return json.loads(lines[-1])
            time.sleep(.01)
        self.fail('Native bridge acknowledgment missing')

    def test_idle_native_delivery_once_and_unload_cleanup(self):
        ctx=self.setup();ctx.hooks['on_session_start'](session_id='native-id')
        self.spawn.assert_not_called()
        ctx.hooks['on_session_end'](session_id='native-id')
        child=self.children[0];child.push()
        self.assertEqual(self.wait_ack(child),{'accepted':True})
        self.assertEqual(len(ctx.calls),1)
        self.assertEqual(ctx.calls[0][1],{'session_key':'native-id'})
        ctx.unload();self.assertTrue(child.terminated)

    def test_busy_work_uses_safe_point_hooks_without_native_interruption(self):
        ctx=self.setup();ctx.hooks['on_session_end'](session_id='native-id');child=self.children[0]
        ctx.hooks['pre_llm_call'](session_id='native-id')
        child.push()
        self.assertEqual(self.wait_ack(child),{'accepted':False})
        with patch.object(self.module,'inbox_due',return_value=True),patch.object(self.module,'claim',return_value=[{'at':time.time(),'text':'Busy follow-up'}]):
            result=ctx.hooks['transform_tool_result'](session_id='native-id',result='Tool completed')
        self.assertIn('Busy follow-up',result);self.assertEqual(ctx.calls,[])

    def test_gateway_consent_is_checked_before_any_claim(self):
        ctx=self.setup(surface='tui',allowed=False)
        ctx.hooks['on_session_end'](session_id='native-id',session_key='route-key')
        self.spawn.assert_not_called()
        self.assertIn('allow_gateway_injection',str(self.emit.call_args))

    def test_gateway_backed_cli_cannot_bypass_gateway_consent(self):
        ctx=self.setup(surface='cli',gateway_backed=True,allowed=False)
        ctx.hooks['on_session_end'](session_id='native-id',session_key='route-key')
        self.spawn.assert_not_called()
        self.assertIn('allow_gateway_injection',str(self.emit.call_args))

    def test_gateway_routes_by_captured_key_and_finalize_stops_request(self):
        ctx=self.setup(surface='tui',allowed=True)
        ctx.hooks['on_session_end'](session_id='native-id',session_key='route-key')
        child=self.children[0];child.push();self.wait_ack(child)
        self.assertEqual(ctx.calls[0][1],{'session_key':'route-key'})
        ctx.hooks['on_session_finalize'](session_id='native-id');self.assertTrue(child.terminated)

    def test_rejection_releases_delivery_and_cli_reset_stops_old_routes(self):
        ctx=self.setup(accepted=False)
        ctx.hooks['on_session_end'](session_id='native-id');child=self.children[0];child.push()
        self.assertEqual(self.wait_ack(child),{'accepted':False})
        ctx.hooks['on_session_reset']();self.assertTrue(child.terminated)
