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
        self.assertTrue(self.events()[0]['data']['user_turn'])
        self.assertNotIn('user_turn',self.events()[1]['data'])
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
    def test_install_opts_the_host_into_a_readable_label_once(self):
        config=self.root/'reporter-env'
        home=self.root/'pinned'
        settings=home/'.codex/hooks.json'; settings.parent.mkdir(parents=True); settings.write_text('{"hooks":{}}')
        with patch.dict(os.environ,{'AXON_CONFIG_FILE':str(config),'AXON_ACTIVITY_HOME':str(self.root/'state')}):
            # A dry run reports the label it would add and writes nothing.
            plan=manage.install('codex',home,apply=False)
            self.assertEqual(plan['machine_label'],{'value':'auto','path':str(config),'written':False,'hostname':plan['machine_label']['hostname']})
            self.assertFalse(config.exists())
            manage.install('codex',home,apply=True)
            self.assertEqual(config.read_text(),'AXON_MACHINE=auto\n')
            self.assertEqual(config.stat().st_mode & 0o777,0o600)
            # Repeat installs never duplicate the setting.
            manage.install('codex',home,apply=True)
            self.assertEqual(config.read_text(),'AXON_MACHINE=auto\n')
            # A pinned label and unrelated settings are left exactly as written.
            original='export AXON_BROKER=journal.example\nAXON_MACHINE=workstation\n'
            config.write_text(original)
            self.assertEqual(manage.install('codex',home,apply=True)['machine_label'],
                             {'value':'workstation','path':str(config),'written':False,'hostname':manage.hostname_preview()})
            self.assertEqual(config.read_text(),original)

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
    def test_claude_usage_collector_runs_from_bundle_without_shell(self):
        package=manage.build('claude',self.root/'claude')
        collector=package/'usage/bin/collect'
        self.assertEqual(collector.read_text().split('\n',1)[0],'#!'+sys.executable)
        self.assertEqual(json.loads((package/'usage/hooks/hooks.json').read_text()),{'modules':['./register.ts']})
        reset=time.time()+3600
        limits={'rate_limits':{'five_hour':{'used_percentage':23.5,'resets_at':reset},'seven_day':{'used_percentage':41,'resets_at':None}},'transcript_path':'PRIVATE'}
        result=subprocess.run([str(collector)],input=json.dumps(limits),text=True,capture_output=True,env=self.env,check=True)
        self.assertEqual((result.stdout,result.stderr),('',''))
        with sqlite3.connect(self.root/'state/outbox.sqlite3') as db:
            topic,payload=db.execute('SELECT topic,payload FROM outbox').fetchone()
        self.assertEqual(topic,'axon/resources/claude/primary')
        self.assertNotIn('PRIVATE',payload)
        windows=json.loads(payload)['windows']
        self.assertEqual([(w['name'],w['used_percent'],w['resets_at']) for w in windows],[('short',23.5,reset),('weekly',41,None)])
        subprocess.run([str(collector)],input='not json',text=True,capture_output=True,env=self.env,check=True)
    def test_claude_install_registers_usage_plugin_dir_once(self):
        settings=self.root/'.claude/settings.json'; settings.parent.mkdir()
        stale=str(self.root/'old/.local/share/axon/plugins/claude/usage')
        settings.write_text(json.dumps({'env':{'KEEP':'1','CLAUDE_CODE_PLUGIN_DIRS':os.pathsep.join(['/mine/mod',stale])}}))
        manage.install('claude',self.root,apply=True)
        manage.install('claude',self.root,apply=True)
        env=json.loads(settings.read_text())['env']
        usage=str(self.root.resolve()/'.local/share/axon/plugins/claude/usage')
        self.assertEqual(env['CLAUDE_CODE_PLUGIN_DIRS'],os.pathsep.join(['/mine/mod',usage]))
        self.assertEqual(env['KEEP'],'1')
        self.assertTrue((Path(usage)/'.claude-plugin/plugin.json').exists())


class HermesTests(unittest.TestCase):
    def setUp(self):
        # Never read the developer's saved settings, reach a real journal server, or write the real spool.
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher=patch.dict(os.environ,{'AXON_ACTIVITY_HOME':str(Path(self.tmp.name)/'state'),
                                       'AXON_CONFIG_FILE':str(Path(self.tmp.name)/'missing-env'),
                                       'AXON_SERVER':'http://127.0.0.1:9'})
        patcher.start(); self.addCleanup(patcher.stop)
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
    def test_dashboard_messages_inject_at_turn_tool_result_and_stop(self):
        hooks={}
        class Context:
            def register_hook(self,name,callback): hooks[name]=callback
        self.module.register(Context())
        message={'at':time.time(),'text':'Push to the staging branch'}
        with patch.object(self.module,'lifecycle'), patch.object(self.module,'inbox_due',return_value=True), patch.object(self.module,'claim',return_value=[message]) as claim:
            self.assertIn('Push to the staging branch',hooks['pre_llm_call'](session_id='chat-one')['context'])
            self.assertEqual(claim.call_args.args[:2],('hermes','chat-one'))
            transformed=hooks['transform_tool_result'](result='{"ok": true}',session_id='chat-one')
            self.assertTrue(transformed.startswith('{"ok": true}'))
            self.assertIn('Push to the staging branch',transformed)
            verdict=hooks['pre_verify'](session_id='chat-one')
            self.assertEqual(verdict['action'],'continue')
            self.assertIn('Push to the staging branch',verdict['message'])
        # No pending input: the tool result passes through untouched.
        with patch.object(self.module,'lifecycle'), patch.object(self.module,'inbox_due',return_value=True), patch.object(self.module,'claim',return_value=[]):
            self.assertNotIn('Axon dashboard',hooks['pre_llm_call'](session_id='chat-one')['context'])
            self.assertIsNone(hooks['transform_tool_result'](result='plain',session_id='chat-one'))
            self.assertIsNone(hooks['pre_verify'](session_id='chat-one'))
        # An unreachable journal never blocks the model, the tool loop, or the stop.
        with patch.object(self.module,'lifecycle'), patch.object(self.module,'inbox_due',side_effect=OSError('offline')), patch.object(self.module,'claim',side_effect=OSError('offline')):
            self.assertIn('context',hooks['pre_llm_call'](session_id='chat-one'))
            self.assertIsNone(hooks['transform_tool_result'](result='plain',session_id='chat-one'))
            self.assertIsNone(hooks['pre_verify'](session_id='chat-one'))
    def test_tool_result_without_session_or_string_passes_through(self):
        hooks={}
        class Context:
            def register_hook(self,name,callback): hooks[name]=callback
        self.module.register(Context())
        with patch.object(self.module,'claim') as claim:
            self.assertIsNone(hooks['transform_tool_result'](result='plain',session_id=''))
            self.assertIsNone(hooks['transform_tool_result'](result={'ok':True},session_id='chat-one'))
            claim.assert_not_called()


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
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.journal,token='t'*20,allow_legacy_claims=False))
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.env.update(AXON_SERVER=f'http://127.0.0.1:{self.server.server_port}',AXON_MACHINE='workstation')
        credential=self.journal.mint_host_credential('workstation')['credential']
        credential_file=self.root/'host-credential';credential_file.write_text(credential);credential_file.chmod(0o600)
        self.env['AXON_HOST_CREDENTIAL_FILE']=str(credential_file)
        self.package=manage.build('claude',self.root/'claude')
    def address(self,session='a'):
        with patch.dict(os.environ,{k:v for k,v in self.env.items() if k.startswith('AXON_')}):
            source,key,_=report.identity('claude',session)
        self.journal.ingest(dict(version=1,source=source,session=key,id='s-'+session,at=time.time(),kind='start',data={'agent':'claude','machine':'workstation'}))
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

    def codex_address(self,session='c'):
        with patch.dict(os.environ,{k:v for k,v in self.env.items() if k.startswith('AXON_')}):
            source,key,_=report.identity('codex',session)
        self.journal.ingest(dict(version=1,source=source,session=key,id='s-'+session,at=time.time(),kind='start',data={'agent':'codex','machine':'workstation'}))
        return source,key

    def test_codex_takes_input_in_turn_and_answers_late_input_at_turn_end(self):
        codex=manage.build('codex',self.root/'codex')
        source,key=self.codex_address()
        self.send(source,key,'Check the staging logs too')
        output=json.loads(self.hook(codex,'PostToolUse',session='c',tool_name='exec').stdout)['hookSpecificOutput']
        self.assertIn('Check the staging logs too',output['additionalContext'])
        self.assertEqual(self.hook(codex,'Stop',session='c').stdout,'','nothing left to answer, so the turn ends')
        self.send(source,key,'And bump the version')
        stop=json.loads(self.hook(codex,'Stop',session='c').stdout)
        self.assertEqual(stop['decision'],'block')
        self.assertIn('And bump the version',stop['reason'])
        self.assertEqual(self.hook(codex,'Stop',session='c',stop_hook_active=True).stdout,'','a claimed message is answered once')

    def test_codex_has_no_idle_listener(self):
        codex=manage.build('codex',self.root/'codex')
        self.codex_address()
        result=subprocess.run([sys.executable,str(codex/'hooks/run.py')],input=json.dumps({'session_id':'c','hook_event_name':'Stop'}),
                              text=True,capture_output=True,env=self.env,timeout=10)
        self.assertEqual((result.returncode,result.stderr),(0,''))
        self.assertFalse(any(e['data'].get('listening_until',0)>time.time() for e in self.events()))

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


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.home=self.root/'home'
        for d in ('opencode/bin','npm-global/bin'):
            (self.home/('.local' if d.startswith('local') else f'.{d.split("/")[0]}')/d.split('/')[1]).mkdir(parents=True,exist_ok=True)
        fake=self.home/'.opencode/bin/opencode'
        fake.write_text('#!/bin/sh\n'); fake.chmod(0o755)
        self.env={'AXON_ACTIVITY_HOME':str(self.root/'state'),'PATH':str(self.home/'.opencode/bin')}
    def tearDown(self):
        self.tmp.cleanup()
    def test_sync_installs_detected_agents_stamps_and_skips(self):
        with patch.dict(os.environ,self.env):
            report=manage.sync(self.home,apply=True)
            self.assertEqual(report['agents'],['opencode'])
            self.assertEqual(report['installed'],['opencode']); self.assertEqual(report['failed'],{})
            self.assertTrue((self.home/'.config/opencode/plugins/axon-activity.js').is_symlink())
            stamp=Path(self.env['AXON_ACTIVITY_HOME'])/'hooks-synced'
            self.assertTrue(stamp.is_file())
            again=manage.sync(self.home,apply=True)
            self.assertTrue(again['skipped'])
            forced=manage.sync(self.home,apply=True,force=True)
            self.assertFalse(forced['skipped'])
            self.assertEqual(forced['installed'],['opencode'])
    def test_sync_dry_run_writes_nothing(self):
        with patch.dict(os.environ,self.env):
            report=manage.sync(self.home,apply=False)
            self.assertEqual(report['installed'],[])
        self.assertFalse((self.home/'.config/opencode').exists())
        self.assertFalse((Path(self.env['AXON_ACTIVITY_HOME'])/'hooks-synced').exists())
    def test_sync_failure_is_isolated_and_blocks_the_stamp(self):
        # Malformed agent settings fail only that agent (read before any
        # change); the stamp stays unwritten so the next run retries.
        (self.home/'.codex').mkdir(parents=True)
        fake=self.home/'.local/bin/codex'; fake.parent.mkdir(parents=True,exist_ok=True)
        fake.write_text('#!/bin/sh\n'); fake.chmod(0o755)
        (self.home/'.codex/hooks.json').write_text('not json at all')
        with patch.dict(os.environ,self.env):
            report=manage.sync(self.home,apply=True)
            self.assertEqual(sorted(report['agents']),['codex','opencode'])
            self.assertIn('codex',report['failed'])
            self.assertEqual(report['installed'],['opencode'])
        self.assertFalse((Path(self.env['AXON_ACTIVITY_HOME'])/'hooks-synced').exists())

    def test_sync_adds_the_host_label_even_when_hooks_are_current(self):
        config=self.root/'reporter-env'
        with patch.dict(os.environ,{**self.env,'AXON_CONFIG_FILE':str(config)}):
            manage.sync(self.home,apply=True)
            config.unlink()
            self.assertTrue(manage.sync(self.home,apply=True)['skipped'])
            self.assertEqual(manage.read_machine_label(config),'auto')
            # A dry run adds nothing, so a bootstrap loop cannot write unexpectedly.
            config.unlink()
            self.assertTrue(manage.sync(self.home,apply=False)['skipped'])
            self.assertFalse(config.exists())

    def test_sync_cli_skip_path_exits_zero(self):
        # main() must tolerate the 'skipped' report shape (no failure map).
        env={**os.environ,'PATH':str(self.home/'.opencode/bin'),'AXON_ACTIVITY_HOME':str(self.root/'state')}
        run=lambda: subprocess.run([sys.executable,str(ROOT/'plugins/tools/manage.py'),'sync','--apply','--home',str(self.home)],
                                   capture_output=True,text=True,env=env)
        first=run(); self.assertEqual(first.returncode,0,first.stderr)
        self.assertEqual(json.loads(first.stdout)['installed'],['opencode'])
        second=run(); self.assertEqual(second.returncode,0,second.stderr)
        self.assertTrue(json.loads(second.stdout)['skipped'])


class TimerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.home=self.root/'home'
        self.env=patch.dict(os.environ,{'AXON_ACTIVITY_HOME':str(self.root/'state'),'PATH':'/opt/agents/bin:/usr/bin'})
        self.env.start()
    def tearDown(self):
        self.env.stop(); self.tmp.cleanup()
    def git(self,repo,*args):
        return subprocess.run(['git','-C',str(repo),*args],capture_output=True,text=True,check=True).stdout.strip()
    def commit(self,repo,name):
        (repo/name).write_text(name)
        self.git(repo,'add',name)
        self.git(repo,'-c','user.name=t','-c','user.email=t@example.invalid','-c','commit.gpgsign=false','commit','-qm',name)
        return self.git(repo,'rev-parse','HEAD')
    def clones(self):
        """An upstream with two commits and a managed clone still at the first."""
        origin=self.root/'origin'; origin.mkdir(); self.git(origin,'init','-q')
        first=self.commit(origin,'a')
        clone=self.root/'clone'
        subprocess.run(['git','clone','-q',str(origin),str(clone)],check=True)
        return origin,clone,first,self.commit(origin,'b')
    def test_systemd_units_run_update_on_the_interval(self):
        files=manage.timer_files(self.home,20,'Linux')
        units=self.home/'.config/systemd/user'
        self.assertEqual(set(files),{units/'axon-sync.service',units/'axon-sync.timer'})
        service=files[units/'axon-sync.service']
        self.assertIn(f'ExecStart={sys.executable} {manage.ROOT}/plugins/tools/manage.py update --apply',service)
        self.assertIn('Environment=PATH=/opt/agents/bin:/usr/bin',service)
        self.assertIn('Type=oneshot',service)
        self.assertIn('OnUnitActiveSec=20min',files[units/'axon-sync.timer'])
        self.assertIn('WantedBy=timers.target',files[units/'axon-sync.timer'])
    def test_launchd_plist_is_valid_and_runs_update(self):
        import plistlib
        (path,text),=manage.timer_files(self.home,15,'Darwin').items()
        self.assertEqual(path,self.home/'Library/LaunchAgents/org.axon.sync.plist')
        plist=plistlib.loads(text.encode())
        self.assertEqual(plist['Label'],'org.axon.sync')
        self.assertEqual(plist['ProgramArguments'],
                         [sys.executable,str(manage.ROOT/'plugins/tools/manage.py'),'update','--apply'])
        self.assertEqual(plist['StartInterval'],900)
        self.assertEqual(plist['EnvironmentVariables']['PATH'],'/opt/agents/bin:/usr/bin')
        self.assertEqual(plist['StandardOutPath'],str(self.root/'state/sync.log'))
    def test_install_timer_plan_writes_nothing_and_apply_enables(self):
        calls=[]
        for system in ('Linux','Darwin'):
            plan=manage.install_timer(self.home,False,system=system,run=lambda *a,**k: calls.append(a))
            self.assertFalse(plan['apply']); self.assertTrue(plan['commands'])
        self.assertFalse(self.home.exists()); self.assertEqual(calls,[])
        plan=manage.install_timer(self.home,True,system='Linux',run=lambda c,**k: calls.append(c))
        self.assertTrue(all(Path(f).is_file() for f in plan['files']))
        self.assertEqual(calls[-1],['systemctl','--user','enable','--now','axon-sync.timer'])
        with self.assertRaises(ValueError): manage.timer_files(self.home,0,'Linux')
    def test_update_pulls_but_never_past_the_server_commit(self):
        origin,clone,first,second=self.clones()
        synced=[]
        sync=lambda home,root: synced.append(self.git(root,'rev-parse','HEAD')) or {'skipped':False}
        # Server still runs the first commit: the clone is held there.
        report=manage.update(self.home,True,clone,server=lambda: first,sync_fn=sync)
        self.assertTrue(report['ok']); self.assertTrue(report['held_at_server'])
        self.assertEqual((report['revision'],synced),(first,[first]))
        # Once the server deploys the second commit, the host follows.
        report=manage.update(self.home,True,clone,server=lambda: second,sync_fn=sync)
        self.assertEqual((report['revision'],report['held_at_server']),(second,False))
        # A server ahead of the clone or out of reach does not hold anything back.
        third=self.commit(origin,'c')
        report=manage.update(self.home,True,clone,server=lambda: None,sync_fn=sync)
        self.assertEqual((report['revision'],report['ok']),(third,True))
        self.assertEqual(synced,[first,second,third])
    def test_update_refuses_unknown_server_commit_and_dirty_clone(self):
        _,clone,first,second=self.clones()
        sync=lambda home,root: self.fail('must not sync')
        report=manage.update(self.home,True,clone,server=lambda: 'f'*40,sync_fn=sync)
        self.assertFalse(report['ok']); self.assertIn('not in this clone',report['error'])
        (clone/'a').write_text('edited')
        report=manage.update(self.home,True,clone,server=lambda: second,sync_fn=sync)
        self.assertIn('local changes',report['error'])
        self.assertEqual(self.git(clone,'rev-parse','HEAD'),first)
    def test_update_status_file_records_success_and_failure(self):
        _,clone,first,second=self.clones()
        self.assertIsNone(manage.status(self.home))
        manage.update(self.home,True,clone,server=lambda: second,sync_fn=lambda h,r: {})
        current=manage.status(self.home)
        self.assertEqual({k:current[k] for k in ('revision','ok','error')},{'revision':second,'ok':True,'error':None})
        self.assertIn('at',current)
        def broken(home,root): raise RuntimeError('sync failed: codex')
        manage.update(self.home,True,clone,server=lambda: second,sync_fn=broken)
        self.assertEqual(manage.status(self.home)['error'],'sync failed: codex')
        self.assertFalse(manage.status(self.home)['ok'])
    def test_update_plan_changes_nothing(self):
        _,clone,first,second=self.clones()
        report=manage.update(self.home,False,clone,server=lambda: second,sync_fn=lambda h,r: self.fail('synced'))
        self.assertEqual(report,{'root':str(clone),'revision':first,'server_commit':second,'apply':False})
        self.assertEqual(self.git(clone,'rev-parse','HEAD'),first)
        self.assertIsNone(manage.status(self.home))


if __name__=='__main__': unittest.main()
