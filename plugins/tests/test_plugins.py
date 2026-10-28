import importlib.util
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
for project in ('packages/python/axon_protocol', 'packages/python/axon_reporter', 'server', 'clients/query', 'plugins/tools'):
    sys.path.insert(0, str(ROOT / project))
import manage
from axon_reporter import plugin_runtime, report
from axon_server.journal import Journal, handler
from http.server import ThreadingHTTPServer
import threading
import time
from urllib.request import Request, urlopen


def load(path,name):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        # Never read the developer's saved settings or reach a real journal server.
        self.env={**os.environ,'AXON_ACTIVITY_HOME':str(self.root/'state'),'AXON_MOSQUITTO_PUB':'/missing/axon-pub','PYTHONDONTWRITEBYTECODE':'1',
                  'AXON_CONFIG_FILE':str(self.root/'missing-env'),'AXON_SERVER':'http://127.0.0.1:9'}
    def tearDown(self):
        self.tmp.cleanup()
    def hook(self,package,event,session='a',**extra):
        data={'session_id':session,'hook_event_name':event,'cwd':'/projects/glass',**extra}
        return subprocess.run([sys.executable,str(package/'hooks/run.py')],input=json.dumps(data),text=True,capture_output=True,env=self.env,check=True)
    def events(self):
        with sqlite3.connect(self.root/'state/outbox.sqlite3') as db:
            return [json.loads(row[0]) for row in db.execute('SELECT payload FROM outbox ORDER BY seq')]
    def test_both_bundles_are_standalone_and_context_is_session_scoped(self):
        for agent in ('codex','claude'):
            package=manage.build(agent,self.root/agent)
            result=self.hook(package,'SessionStart',session='safe; echo SECRET',prompt='PRIVATE PROMPT',tool_input={'cmd':'PRIVATE COMMAND'})
            output=json.loads(result.stdout)['hookSpecificOutput']
            self.assertEqual(output['hookEventName'],'SessionStart')
            text=output['additionalContext']
            self.assertIn("'safe; echo SECRET'",text)
            self.assertIn(str(package/'runtime/axon_reporter/report.py'),text)
            self.assertNotIn('PRIVATE',text)
            self.assertEqual(result.stderr,'')
            self.assertNotIn('PRIVATE',json.dumps(self.events()))
    def test_rebuild_removes_obsolete_runtime_files(self):
        package = manage.build('codex', self.root/'codex')
        (package/'runtime/journal.py').write_text('obsolete server')
        manage.build('codex', package)
        self.assertFalse((package/'runtime/journal.py').exists())

    def test_generated_report_command_runs_without_source_imports(self):
        package = manage.build('codex', self.root/'codex')
        output = json.loads(self.hook(package, 'SessionStart').stdout)
        text = output['hookSpecificOutput']['additionalContext']
        command = text.split('command prefix: ',1)[1].split('. Append:',1)[0]
        args = shlex.split(command)+['finished','--outcome','Verified bundle','--evidence','test: isolated command']
        env = {k:v for k,v in self.env.items() if k != 'PYTHONPATH'}
        result = subprocess.run(args,cwd=self.root,env=env,capture_output=True,text=True,check=True)
        self.assertTrue(json.loads(result.stdout)['queued'])
        self.assertEqual(self.events()[-1]['data']['outcome'], 'Verified bundle')

    def test_report_command_retains_hook_settings_in_clean_shell(self):
        self.env.update(AXON_BROKER='journal.example', AXON_PORT='1884', AXON_MACHINE='workstation', AXON_SOURCE='codex-stable', UNRELATED_SECRET='never-copy')
        package=manage.build('codex',self.root/'codex')
        text=json.loads(self.hook(package,'SessionStart').stdout)['hookSpecificOutput']['additionalContext']
        command=text.split('command prefix: ',1)[1].split('. Append:',1)[0]
        self.assertNotIn('never-copy',command)
        args=shlex.split(command)+['progress','--step','Clean shell delivery']
        clean={k:v for k,v in self.env.items() if not k.startswith('AXON_')}
        clean['AXON_MOSQUITTO_PUB']='/missing/axon-pub'
        subprocess.run(args,cwd=self.root,env=clean,capture_output=True,text=True,check=True)
        event=self.events()[-1]
        self.assertEqual(event['source'],'codex-stable')
        self.assertEqual(event['data']['machine'],'workstation')
        self.assertIn('AXON_BROKER=journal.example',command)
        self.assertIn('AXON_PORT=1884',command)

    def test_tools_throttled_but_waiting_lifecycle_not_throttled(self):
        package=manage.build('codex',self.root/'codex')
        self.hook(package,'UserPromptSubmit')
        for event in ('PreToolUse','PostToolUse','PreToolUse','PostToolUse'):
            self.assertEqual(self.hook(package,event,tool_name='exec_command').stdout,'')
        self.hook(package,'Stop')
        self.assertEqual([e['kind'] for e in self.events()],['progress','heartbeat','waiting'])
    def test_prompt_followup_does_not_erase_goal_and_missing_cwd_does_not_erase_project(self):
        with patch.dict(os.environ,self.env), patch.object(report,'drain'):
            report.report('hermes','a','start',{'project':'axon','goal':'Build plugin','step':'Test hooks'})
            plugin_runtime.lifecycle('hermes',{'session_id':'a','hook_event_name':'UserPromptSubmit'})
            plugin_runtime.lifecycle('hermes',{'session_id':'a','hook_event_name':'PostToolUse','tool_name':'terminal'})
        journal=Journal(self.root/'journal.db')
        for event in self.events(): journal.ingest(event)
        row=journal.sessions()[0]
        self.assertEqual((row['goal'],row['project'],row['step']),('Build plugin','axon','Test hooks'))
    def test_config_install_preserves_other_hooks_and_is_idempotent(self):
        for agent in ('codex','claude'):
            home=self.root/agent
            settings=home/f'.{agent}'/('hooks.json' if agent=='codex' else 'settings.json')
            settings.parent.mkdir(parents=True)
            original={'other':{'keep':True},'hooks':{'Stop':[{'matcher':'*','hooks':[
                {'type':'command','command':'python3 /keep/my-hook.py'},
                {'type':'command','command':'python3 /home/me/.local/share/axon/plugins/codex/hooks/run.py'}]}]}}
            settings.write_text(json.dumps(original))
            manage.install(agent,home,apply=True)
            first=json.loads(settings.read_text())
            manage.install(agent,home,apply=True)
            second=json.loads(settings.read_text())
            self.assertEqual(first,second)
            self.assertEqual(second['other'],original['other'])
            commands=[h['command'] for g in second['hooks']['Stop'] for h in g['hooks']]
            self.assertIn('python3 /keep/my-hook.py',commands)
            self.assertFalse(any('/home/me/.local/share/axon/' in c for c in commands))
            self.assertTrue((home/f'.{agent}/skills/axon-activity/SKILL.md').exists())
            self.assertTrue(list((home/'.local/state/axon/install-backups').iterdir()))
    def test_invalid_settings_leave_configuration_and_package_untouched(self):
        settings=self.root/'.codex/hooks.json'; settings.parent.mkdir(); settings.write_text('{"hooks":{"Stop":"invalid"}}')
        with self.assertRaises(ValueError): manage.install('codex',self.root,apply=True)
        self.assertEqual(settings.read_text(),'{"hooks":{"Stop":"invalid"}}')
        self.assertFalse((self.root/'.local/share/axon/plugins').exists())
    def test_malformed_hook_input_fails_open(self):
        package=manage.build('codex',self.root/'codex')
        result=subprocess.run([sys.executable,str(package/'hooks/run.py')],input='not json',text=True,capture_output=True,env=self.env)
        self.assertEqual((result.returncode,result.stdout,result.stderr),(0,'',''))
    def test_finished_without_evidence_is_rejected_before_queueing(self):
        with patch.dict(os.environ,self.env):
            with self.assertRaisesRegex(ValueError,'evidence'):
                report.report('codex','a','finished',{'outcome':'Done'})
        self.assertFalse((self.root/'state/outbox.sqlite3').exists())
    def test_bundles_exclude_server_and_query_client(self):
        for agent in ('codex', 'claude', 'hermes', 'pi', 'opencode'):
            package = manage.build(agent, self.root/agent)
            names = {p.name for p in package.rglob('*.py')}
            self.assertNotIn('journal.py', names)
            self.assertNotIn('query.py', names)
            self.assertTrue((package/'runtime/axon_protocol/__init__.py').exists())
            self.assertTrue((package/'runtime/axon_reporter/report.py').exists())


class HermesTests(unittest.TestCase):
    def setUp(self):
        self.module=load(ROOT/'plugins/hermes/__init__.py','axon_hermes_test')
    def test_registers_observers_and_session_context_without_inspecting_user_text(self):
        hooks={}
        class Context:
            def register_hook(self,name,callback): hooks[name]=callback
        self.module.register(Context())
        with patch.object(self.module,'lifecycle') as lifecycle:
            result=hooks['pre_llm_call'](session_id='chat-one',user_message='SECRET',conversation_history=['SECRET'])
            self.assertNotIn('SECRET',result['context'])
            self.assertIn('chat-one',result['context'])
            hooks['on_session_end'](session_id='chat-one',completed=True)
            self.assertEqual(lifecycle.call_args.args[1]['hook_event_name'],'Stop')
            hooks['on_session_end'](session_id='chat-one',interrupted=True)
            self.assertEqual(lifecycle.call_args.args[1]['hook_event_name'],'Interrupt')
        self.assertIsNone(hooks['pre_llm_call'](user_message='SECRET'))
    def test_shared_runtime_does_not_reuse_generic_report_modules(self):
        self.assertEqual(self.module.report.__name__,'_axon_hermes_runtime.report')

    def test_failures_never_block_model_or_tools(self):
        with patch.object(self.module,'lifecycle',side_effect=OSError('offline')):
            self.assertIn('context',self.module.before_turn(session_id='a'))
            self.assertIsNone(self.module.after_tool(session_id='a',tool_name='terminal',args={'command':'SECRET'}))
    def test_child_parent_link_does_not_transmit_child_goal(self):
        with patch.dict(os.environ, AXON_MACHINE='test-installation'), patch.object(self.module.report,'report') as emit:
            self.module.child_start(child_session_id='child',parent_session_id='parent',child_goal='SECRET')
            self.assertIn('parent',emit.call_args.args[3])
            self.assertNotIn('SECRET',str(emit.call_args))


class PiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.env={**os.environ,'AXON_ACTIVITY_HOME':str(self.root/'state'),'AXON_MOSQUITTO_PUB':'/missing/axon-pub','PYTHONDONTWRITEBYTECODE':'1'}
    def tearDown(self):
        self.tmp.cleanup()
    def test_bundle_is_a_discoverable_pi_package(self):
        package=manage.build('pi',self.root/'pi')
        manifest=json.loads((package/'package.json').read_text())
        self.assertEqual(manifest['pi']['extensions'],['./index.ts'])
        self.assertTrue((package/'index.ts').is_file())
        self.assertTrue((package/'hooks/run.py').is_file())
        self.assertTrue((package/'skills/axon-activity/SKILL.md').is_file())
        manage.build('pi',package)  # recognized package replaces cleanly
        self.assertTrue((package/'runtime/axon_reporter/report.py').is_file())
    def test_hook_is_standalone_fails_open_and_never_leaks_prompts(self):
        package=manage.build('pi',self.root/'pi')
        data={'session_id':'pi; echo SECRET','hook_event_name':'SessionStart','cwd':'/projects/glass',
              'prompt':'PRIVATE PROMPT','systemPrompt':'PRIVATE','event':{'message':'PRIVATE'}}
        result=subprocess.run([sys.executable,str(package/'hooks/run.py')],input=json.dumps(data),
                              text=True,capture_output=True,env=self.env,check=True)
        output=json.loads(result.stdout)['hookSpecificOutput']
        self.assertEqual(output['hookEventName'],'SessionStart')
        self.assertIn("'pi; echo SECRET'",output['additionalContext'])
        self.assertIn(str(package/'runtime/axon_reporter/report.py'),output['additionalContext'])
        self.assertEqual(result.stderr,'')
        self.assertNotIn('PRIVATE',result.stdout)
        with sqlite3.connect(self.root/'state/outbox.sqlite3') as db:
            self.assertNotIn('PRIVATE',str([row[0] for row in db.execute('SELECT payload FROM outbox')]))
        result=subprocess.run([sys.executable,str(package/'hooks/run.py')],input='not json',
                              text=True,capture_output=True,env=self.env)
        self.assertEqual((result.returncode,result.stdout,result.stderr),(0,'',''))
    def test_pi_install_symlinks_extension_and_skill_idempotently(self):
        home=self.root/'home'
        manage.install('pi',home,apply=True)
        extension=home/'.pi/agent/extensions/axon-activity'
        skill=home/'.pi/agent/skills/axon-activity'
        self.assertTrue(extension.is_symlink())
        self.assertTrue((extension/'index.ts').is_file())
        self.assertTrue((skill/'SKILL.md').is_file())
        first=(extension.resolve(),skill.resolve())
        manage.install('pi',home,apply=True)
        self.assertEqual((extension.resolve(),skill.resolve()),first)
        self.assertTrue(list((home/'.local/state/axon/install-backups').iterdir()))
        # The install never invents or rewrites a Pi settings file.
        self.assertFalse((home/'.pi/agent/settings.json').exists())


class InboxTests(unittest.TestCase):
    """Dashboard input reaches Claude sessions through hooks, using a real journal server."""
    hook=PluginTests.hook
    events=PluginTests.events
    def setUp(self):
        PluginTests.setUp(self);self.addCleanup(self.tmp.cleanup)
        self.env['AXON_LISTEN_POLL']='1'
        self.journal=Journal(self.root/'journal.db')
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.journal,token='t'*20))
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.env.update(AXON_SERVER=f'http://127.0.0.1:{self.server.server_port}',AXON_MACHINE='workstation')
        self.package=manage.build('claude',self.root/'claude')
    def address(self,session='a'):
        with patch.dict(os.environ,{k:v for k,v in self.env.items() if k.startswith('AXON_')}):
            source,key,_=report.identity('claude',session)
        self.journal.ingest(dict(version=1,source=source,session=key,id='s-'+session,at=time.time(),kind='start',data={'agent':'claude'}))
        return source,key
    def send(self,source,key,text,token='t'*20):
        request=Request(f'http://127.0.0.1:{self.server.server_port}/sessions/{source}/{key}/messages',method='POST',
                        data=json.dumps({'text':text}).encode(),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        with urlopen(request) as response:return json.load(response)
    def listen(self,session='a',**env):
        data=json.dumps({'session_id':session,'hook_event_name':'Stop'})
        return subprocess.Popen([sys.executable,str(self.package/'hooks/run.py'),'listen'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,text=True,env={**self.env,**env}),data

    def test_idle_listener_wakes_session_with_exit_two(self):
        source,key=self.address()
        proc,data=self.listen()
        proc.stdin.write(data);proc.stdin.close()
        time.sleep(1.5)
        self.assertIsNone(proc.poll())
        self.send(source,key,'Also update the docs')
        self.assertEqual(proc.wait(10),2)
        self.assertIn('Also update the docs',proc.stderr.read())
        self.assertIsNotNone(self.journal.messages(source,key)[0]['delivered_at'])
        self.assertTrue(any(e['data'].get('listening_until',0)>time.time() for e in self.events()))

    def test_newer_listener_or_local_prompt_retires_waiting_listener(self):
        self.address()
        first,data=self.listen();first.stdin.write(data);first.stdin.close()
        time.sleep(1)
        self.hook(self.package,'UserPromptSubmit')
        self.assertEqual(first.wait(10),0)
        self.assertEqual(self.events()[-1]['data']['listening_until'],0)

    def test_in_turn_messages_arrive_as_context_once(self):
        source,key=self.address()
        self.send(source,key,'Use the staging broker')
        output=json.loads(self.hook(self.package,'PostToolUse',tool_name='Bash').stdout)['hookSpecificOutput']
        self.assertEqual(output['hookEventName'],'PostToolUse')
        self.assertIn('Use the staging broker',output['additionalContext'])
        self.assertEqual(self.hook(self.package,'UserPromptSubmit').stdout.count('Use the staging broker'),0)

    def test_installer_keeps_listener_argument_and_async_wake(self):
        stop=manage.merged_config({},'claude',self.package)['hooks']['Stop'][0]['hooks']
        listener=[h for h in stop if h.get('asyncRewake')]
        self.assertEqual(len(listener),1)
        self.assertEqual(shlex.split(listener[0]['command'])[-1],'listen')
        self.assertGreaterEqual(listener[0]['timeout'],plugin_runtime.LISTEN_CAP)
        self.assertTrue(all(shlex.split(h['command'])[1].endswith('/hooks/run.py') for h in stop))


class OpenCodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.env={**os.environ,'AXON_ACTIVITY_HOME':str(self.root/'state'),'AXON_MOSQUITTO_PUB':'/missing/axon-pub','PYTHONDONTWRITEBYTECODE':'1'}
    def tearDown(self):
        self.tmp.cleanup()
    def test_bundle_contains_plugin_module_and_runtime(self):
        package=manage.build('opencode',self.root/'opencode')
        source=(package/'plugin/axon.js').read_text()
        self.assertIn('export default',source)  # v2 definition: default {id, setup}
        self.assertIn('id: "axon-activity"',source)
        self.assertIn('async setup(ctx)',source)
        self.assertIn('async server()',source)  # v1 >= 1.18.29 compatibility
        self.assertTrue((package/'hooks/run.py').is_file())
        self.assertTrue((package/'skills/axon-activity/SKILL.md').is_file())
        manage.build('opencode',package)  # recognized package replaces cleanly
        self.assertTrue((package/'runtime/axon_reporter/report.py').is_file())
    def test_hook_is_standalone_fails_open_and_never_leaks_prompts(self):
        package=manage.build('opencode',self.root/'opencode')
        data={'session_id':'ses_1; echo SECRET','hook_event_name':'UserPromptSubmit','cwd':'/projects/glass',
              'parts':[{'text':'PRIVATE PROMPT'}],'args':{'command':'PRIVATE COMMAND'}}
        result=subprocess.run([sys.executable,str(package/'hooks/run.py')],input=json.dumps(data),
                              text=True,capture_output=True,env=self.env,check=True)
        output=json.loads(result.stdout)['hookSpecificOutput']
        self.assertEqual(output['hookEventName'],'UserPromptSubmit')
        self.assertIn("'ses_1; echo SECRET'",output['additionalContext'])
        self.assertEqual(result.stderr,'')
        self.assertNotIn('PRIVATE',result.stdout)
        with sqlite3.connect(self.root/'state/outbox.sqlite3') as db:
            self.assertNotIn('PRIVATE',str([row[0] for row in db.execute('SELECT payload FROM outbox')]))
        result=subprocess.run([sys.executable,str(package/'hooks/run.py')],input='not json',
                              text=True,capture_output=True,env=self.env)
        self.assertEqual((result.returncode,result.stdout,result.stderr),(0,'',''))
    def test_opencode_install_links_plugin_and_skill_idempotently(self):
        home=self.root/'home'
        manage.install('opencode',home,apply=True)
        plugin=home/'.config/opencode/plugins/axon-activity.js'
        skill=home/'.config/opencode/skills/axon-activity'
        self.assertTrue(plugin.is_symlink())  # realpath must resolve into the package
        self.assertIn('axon-activity',plugin.read_text())
        self.assertTrue((skill/'SKILL.md').is_file())
        first=(plugin.read_bytes(),skill.resolve())
        manage.install('opencode',home,apply=True)
        self.assertEqual((plugin.read_bytes(),skill.resolve()),first)
        self.assertTrue(list((home/'.local/state/axon/install-backups').iterdir()))
        # The install never invents or rewrites an opencode config file.
        self.assertFalse((home/'.config/opencode/opencode.json').exists())


if __name__=='__main__': unittest.main()
