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

SOFTWARE=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(SOFTWARE/'plugins'))
sys.path.insert(0,str(SOFTWARE/'activity-journal'))
import manage
import plugin_runtime
import report
from journal import Journal


def load(path,name):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.env={**os.environ,'AXON_ACTIVITY_HOME':str(self.root/'state'),'AXON_MOSQUITTO_PUB':'/missing/axon-pub','PYTHONDONTWRITEBYTECODE':'1'}
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
            self.assertIn(str(package/'runtime/report.py'),text)
            self.assertNotIn('PRIVATE',text)
            self.assertEqual(result.stderr,'')
            self.assertNotIn('PRIVATE',json.dumps(self.events()))
    def test_tools_throttled_but_waiting_lifecycle_not_throttled(self):
        package=manage.build('codex',self.root/'codex')
        self.hook(package,'UserPromptSubmit')
        for event in ('PreToolUse','PostToolUse','PreToolUse','PostToolUse'):
            self.assertEqual(self.hook(package,event,tool_name='exec_command').stdout,'')
        self.hook(package,'Stop')
        self.assertEqual([e['kind'] for e in self.events()],['progress','heartbeat','waiting'])
    def test_prompt_followup_does_not_erase_goal_and_missing_cwd_does_not_erase_project(self):
        with patch.dict(os.environ,self.env), patch.object(report,'drain'),patch.object(report,'publish_summary'):
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
                {'type':'command','command':'python3 /home/me/.codex/hooks/codex_axon.py'}]}]}}
            settings.write_text(json.dumps(original))
            manage.install(agent,home,apply=True)
            first=json.loads(settings.read_text())
            manage.install(agent,home,apply=True)
            second=json.loads(settings.read_text())
            self.assertEqual(first,second)
            self.assertEqual(second['other'],original['other'])
            commands=[h['command'] for g in second['hooks']['Stop'] for h in g['hooks']]
            self.assertIn('python3 /keep/my-hook.py',commands)
            self.assertFalse(any('codex_axon.py' in c for c in commands))
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
    def test_compatibility_summary_keeps_other_session_running(self):
        with patch.dict(os.environ,self.env), patch.object(report,'drain'),patch.object(report,'publish_summary'):
            report.report('codex','a','start',{'goal':'First task'})
            report.report('codex','b','start',{'goal':'Second task'})
            report.report('codex','b','waiting',{})
        source=report.identity('codex','a')[0]
        with patch.dict(os.environ,self.env),patch.object(report.subprocess,'run') as publish:
            report.publish_summary(self.root/'state/outbox.sqlite3',source)
        args=publish.call_args.args[0]
        payload=json.loads(args[args.index('-m')+1])
        self.assertEqual(payload['state'],'running')
        self.assertEqual(payload['task'],'First task')
        self.assertIn('-r',args)


class HermesTests(unittest.TestCase):
    def setUp(self):
        self.module=load(SOFTWARE/'hermes-hooks/__init__.py','axon_hermes_test')
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
        with patch.object(self.module.report,'report') as emit:
            self.module.child_start(child_session_id='child',parent_session_id='parent',child_goal='SECRET')
            self.assertIn('parent',emit.call_args.args[3])
            self.assertNotIn('SECRET',str(emit.call_args))


if __name__=='__main__': unittest.main()
