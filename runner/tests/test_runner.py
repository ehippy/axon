import json
import os
from pathlib import Path
import stat
import subprocess
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
        # The codex executor drops per-job logs under this; keep them in the sandbox.
        self.addCleanup(os.environ.__setitem__,'AXON_ACTIVITY_HOME',os.environ.get('AXON_ACTIVITY_HOME',''))
        os.environ['AXON_ACTIVITY_HOME']=str(self.root)
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

    def config_for(self,allow,executable=None,extra=''):
        path=self.root/'runner.toml'
        entries=''.join('[[allow]]\nproject="%s"\npath="%s"\nagents=[%s]\n'
                        % (e[0], e[1], ','.join('"%s"' % a for a in e[2])) for e in allow)
        executables=''.join('%s="%s"\n' % item for item in (executable or {}).items())
        path.write_text('server="%s"\n%s%s%s' % (self.base, extra, entries,
                          ('[executable]\n'+executables) if executable else ''))
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
        job=self.enqueue(agent='opencode')
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

    def stub_codex(self,stdout,code=0):
        """A fake `codex` that records argv and streams the given lines as its exec log."""
        trace=self.root/'codex-argv.json'
        payload=self.root/'codex-out'
        payload.write_text(stdout)
        script=self.root/'fake-codex'
        script.write_text('#!/bin/sh\nprintf "%s" "$@" > %s\ncat %s\nexit %d\n'
                          % ('%s\\n', trace, payload, code))
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return str(script),trace

    def test_allowed_codex_starts_with_verified_flags_and_reports_thread(self):
        stub,trace=self.stub_codex('{"type":"thread.started","thread_id":"01a18791-46d2-7960-8748-3bd03d2f2859"}\n'
                                   '{"type":"turn.started"}\n')
        policy=self.config_for([('sample',str(self.project),['codex'])],executable={'codex':stub})
        job=self.enqueue(agent='codex')
        result,_=self.poll(policy)
        argv=trace.read_text().strip().split('\n')
        self.assertEqual((result['id'],self.outcome(job['id'])['status']), (job['id'],'started'))
        self.assertEqual(argv[:6],['exec','--json','-s','workspace-write','-C',str(self.project)])
        self.assertEqual(argv[6],'--worktree')
        self.assertEqual(argv[7],'Do the thing')
        self.assertEqual(self.outcome(job['id'])['detail'],'thread 01a18791-46d2-7960-8748-3bd03d2f2859')

    def test_codex_sandbox_narrows_from_config(self):
        stub,trace=self.stub_codex('{"type":"thread.started","thread_id":"t-1"}\n')
        path=self.root/'runner.toml'
        path.write_text('server="%s"\n[[allow]]\nproject="sample"\npath="%s"\nagents=["codex"]\nsandbox="read-only"\n[executable]\ncodex="%s"\n'
                        % (self.base,self.project,stub))
        job=self.enqueue(agent='codex')
        self.poll(config.load(path))
        argv=trace.read_text().strip().split('\n')
        self.assertEqual(argv[:4],['exec','--json','-s','read-only'])
        self.assertEqual(self.outcome(job['id'])['status'],'started')

    def test_codex_bypass_sandbox_refused(self):
        stub,_=self.stub_codex('{"type":"thread.started","thread_id":"t-1"}\n')
        path=self.root/'runner.toml'
        path.write_text('server="%s"\n[[allow]]\nproject="sample"\npath="%s"\nagents=["codex"]\nsandbox="danger-full-access"\n[executable]\ncodex="%s"\n'
                        % (self.base,self.project,stub))
        job=self.enqueue(agent='codex')
        self.poll(config.load(path))
        self.assertEqual((self.outcome(job['id'])['status'],self.outcome(job['id'])['detail']),
                         ('refused','bad_sandbox'))

    def test_codex_failure_before_thread_reports_launch_failed(self):
        stub,trace=self.stub_codex('error: workspace not trusted\n',code=2)
        policy=self.config_for([('sample',str(self.project),['codex'])],executable={'codex':stub})
        job=self.enqueue(agent='codex')
        self.poll(policy)
        outcome=self.outcome(job['id'])
        self.assertEqual(outcome['status'],'refused')
        self.assertIn('launch_failed',outcome['detail'])
        self.assertIn('workspace not trusted',outcome['detail'])

    def test_codex_ignores_non_json_noise_before_thread(self):
        stub,_=self.stub_codex('warning: updating\r\n{"type":"thread.started","thread_id":"t-2"}\n')
        policy=self.config_for([('sample',str(self.project),['codex'])],executable={'codex':stub})
        job=self.enqueue(agent='codex')
        self.poll(policy)
        self.assertEqual(self.outcome(job['id'])['detail'],'thread t-2')

    def test_idle_claim_returns_none(self):
        result,_=self.poll(self.config_for([]))
        self.assertIsNone(result)

    def git_init(self,path):
        for cmd in (['git','init','-q','-b','main'],['git','commit','-q','--allow-empty','-m','seed']):
            subprocess.run(cmd,cwd=path,check=True,capture_output=True)

    def test_allowed_pi_starts_with_verified_flags_and_reports_session(self):
        stub=self.stub('fake-pi')
        trace=Path(stub).with_name('fake-pi-argv.json')
        policy=self.config_for([('sample',str(self.project),['pi'])],executable={'pi':stub})
        job=self.enqueue(agent='pi',worktree=False)
        self.poll(policy)
        argv=trace.read_text().strip().split('\n')
        self.assertEqual((self.outcome(job['id'])['id'],self.outcome(job['id'])['status']),
                         (job['id'],'started'))
        self.assertEqual(argv[:3],['-p','--session-id','axon-'+job['id'][:8]])
        self.assertEqual(argv[3],'Do the thing')
        self.assertEqual(self.outcome(job['id'])['detail'],'session axon-'+job['id'][:8])

    def test_pi_worktree_job_runs_in_runner_managed_worktree(self):
        self.git_init(self.project)
        trace=self.root/'pi-pwd'
        script=self.root/'fake-pi'
        script.write_text('#!/bin/sh\npwd > %s\nexit 0\n' % trace)
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        wt_root=self.root/'wt'
        policy=self.config_for([('sample',str(self.project),['pi'])],executable={'pi':str(script)},
                                extra='worktree_root="%s"\n' % wt_root)
        job=self.enqueue(agent='pi',worktree=True)
        self.poll(policy)
        outcome=self.outcome(job['id'])
        self.assertEqual(outcome['status'],'started')
        wt=wt_root/('project-'+job['id'][:8])
        self.assertTrue(wt.is_dir())
        # the stub records its cwd: the launch happened in the per-job worktree,
        # not the main checkout
        pwd=Path(trace).read_text().strip()
        self.assertIn('project-'+job['id'][:8],pwd)
        self.assertNotEqual(Path(pwd),self.project.resolve())
        listed=subprocess.run(['git','worktree','list'],cwd=self.project,capture_output=True,text=True).stdout
        self.assertIn('axon-'+job['id'][:8],listed)

    def test_pi_worktree_failure_on_non_git_project_refuses(self):
        stub=self.stub('fake-pi')
        policy=self.config_for([('sample',str(self.project),['pi'])],executable={'pi':stub},
                                extra='worktree_root="%s"\n' % (self.root/'wt2'))
        job=self.enqueue(agent='pi',worktree=True)
        self.poll(policy)
        outcome=self.outcome(job['id'])
        self.assertEqual(outcome['status'],'refused')
        self.assertIn('worktree_setup_failed',outcome['detail'])

    def test_pi_immediate_crash_reports_launch_failed(self):
        script=self.root/'bad-pi'
        script.write_text('#!/bin/sh\necho "error: no provider credentials" >&2\nexit 1\n')
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        policy=self.config_for([('sample',str(self.project),['pi'])],executable={'pi':str(script)})
        job=self.enqueue(agent='pi',worktree=False)
        self.poll(policy)
        outcome=self.outcome(job['id'])
        self.assertEqual(outcome['status'],'refused')
        self.assertIn('launch_failed',outcome['detail'])
        self.assertIn('no provider credentials',outcome['detail'])


if __name__=='__main__':
    unittest.main()
