import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[2]
for package in ('server', 'packages/python/axon_protocol', 'runner'):
    sys.path.insert(0, str(ROOT/package))
from axon_server.journal import Journal, handler
from axon_runner import cli, config

TOKEN='test-input-token'


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.journal=Journal(self.root/'journal.db')
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.journal,token=TOKEN))
        self.addCleanup(self.server.server_close)
        thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        thread.start();self.addCleanup(self.server.shutdown)
        self.base='http://127.0.0.1:%d' % self.server.server_address[1]
        self.credential=self.journal.mint_host_credential('runner-test')['credential']
        self.project=self.root/'project'
        self.project.mkdir()

    def stub(self,name,stdout='',code=0):
        """A fake agent CLI recording the exact argv it was launched with."""
        trace=self.root/(name+'-argv.json')
        script=self.root/name
        script.write_text('#!/bin/sh\nprintf "%s" "$@" > %s\n%s\nexit %d\n'
                          % ('%s\\n', trace, ('echo '+repr(stdout)) if stdout else '', code))
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return str(script)

    def config_for(self,allow,executable=None):
        path=self.root/'runner.toml'
        entries=''.join('[[allow]]\nproject="%s"\npath="%s"\nagents=[%s]\n'
                        % (e[0], e[1], ','.join('"%s"' % a for a in e[2])) for e in allow)
        executables=''.join('%s="%s"\n' % item for item in (executable or {}).items())
        path.write_text('server="%s"\n%s%s' % (self.base, entries, ('[executable]\n'+executables) if executable else ''))
        return config.load(path)

    def enqueue(self,**overrides):
        job={'agent':'claude','project':'sample','prompt':'Do the thing','worktree':True}
        return self.journal.enqueue_job('runner-test',{**job,**overrides})

    def outcome(self,job_id):
        return next(j for j in self.journal.jobs('runner-test') if j['id']==job_id)

    def poll(self,policy):
        logs=[]
        outcome=cli.run_once(self.base,'runner-test',self.credential,policy,0,log=logs.append)
        return outcome,logs

    def test_no_config_file_refuses_everything(self):
        job=self.enqueue()
        result,_=self.poll(config.load(self.root/'missing.toml'))
        self.assertEqual((result['id'],self.outcome(job['id'])['status'],self.outcome(job['id'])['detail']),
                         (job['id'],'refused','not_allowed'))

    def test_allowlist_matches_project_and_agent_exactly(self):
        policy=self.config_for([('sample',str(self.project),['claude'])])
        other=self.journal.enqueue_job('runner-test',{'agent':'claude','project':'elsewhere','prompt':'x'})
        wrong_agent=self.journal.enqueue_job('runner-test',{'agent':'codex','project':'sample','prompt':'x'})
        self.poll(policy);self.poll(policy)
        self.assertEqual(self.outcome(other['id'])['detail'],'not_allowed')
        self.assertEqual(self.outcome(wrong_agent['id'])['detail'],'not_allowed')

    def test_allowed_project_without_executor_refuses_not_drops(self):
        policy=self.config_for([('sample',str(self.project),[])])
        job=self.enqueue(agent='pi')
        self.poll(policy)
        self.assertEqual(self.outcome(job['id'])['status'],'refused')
        self.assertEqual(self.outcome(job['id'])['detail'],'no_executor')

    def test_allowed_claude_starts_with_verified_flags_and_reports_started(self):
        stub=self.stub('fake-claude')
        trace=Path(stub).with_name('fake-claude-argv.json')
        policy=self.config_for([('sample',str(self.project),['claude'])],executable={'claude':stub})
        job=self.enqueue()
        # the stub writes argv one per line; check the exact invocation
        result,_=self.poll(policy)
        argv=trace.read_text().strip().split('\n') if trace.exists() else []
        outcome=self.outcome(job['id'])
        self.assertEqual((result['id'],outcome['status']), (job['id'],'started'))
        name='axon-'+job['id'][:8]
        self.assertEqual(argv[:7],['--bg','-n',name,'--permission-mode','dontAsk','-w',name])
        self.assertEqual(argv[7],'Do the thing')
        self.assertEqual(outcome['detail'],'launched as '+name)

    def test_launch_failure_reports_refused_with_tail(self):
        script=self.root/'bad-claude'
        script.write_text('#!/bin/sh\necho "boom: workspace not trusted" >&2\nexit 3\n')
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        policy=self.config_for([('sample',str(self.project),['claude'])],executable={'claude':str(script)})
        job=self.enqueue()
        self.poll(policy)
        outcome=self.outcome(job['id'])
        self.assertEqual(outcome['status'],'refused')
        self.assertIn('launch_failed',outcome['detail'])
        self.assertIn('workspace not trusted',outcome['detail'])

    def test_missing_project_path_refuses(self):
        policy=self.config_for([('sample',str(self.root/'gone'),['claude'])])
        job=self.enqueue()
        self.poll(policy)
        self.assertEqual(self.outcome(job['id'])['detail'],'project_path_missing')

    def test_idle_claim_returns_none(self):
        result,_=self.poll(self.config_for([]))
        self.assertIsNone(result)


if __name__=='__main__':
    unittest.main()
