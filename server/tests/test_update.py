"""The updater deploys only verified server commits, and a failed deploy leaves the previous release live."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('axon_update',ROOT/'server/deploy/axon_update.py')
updater=importlib.util.module_from_spec(spec);spec.loader.exec_module(updater)

class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);base=Path(self.tmp.name)
        self.repo=base/'repo';self.root=base/'server';self.state=base/'state';self.root.mkdir();self.state.mkdir()
        subprocess.run(['git','init','--quiet','--initial-branch','main',str(self.repo)],check=True)
        self.first=self.commit('server/axon_server/journal.py','one')
        with sqlite3.connect(self.state/'activity.sqlite3') as db: db.execute('create table events(id)')
        release=self.root/'releases'/('0.3.0-'+self.first[:7]);release.mkdir(parents=True)
        (self.root/'current').symlink_to(release.relative_to(self.root))
        self.args=updater.parser().parse_args(['--repo',str(self.repo),'--root',str(self.root),'--db',str(self.state/'activity.sqlite3'),
                                               '--backups',str(self.state/'backups'),'--no-self-update'])
        self.restarts=[]
        patcher=patch.object(updater,'build',side_effect=self.fake_build);patcher.start();self.addCleanup(patcher.stop)

    def commit(self,path,text):
        file=self.repo/path;file.parent.mkdir(parents=True,exist_ok=True);file.write_text(text)
        run=lambda *a:subprocess.run(['git','-C',str(self.repo),*a],check=True,capture_output=True,text=True).stdout.strip()
        run('add','-A');run('-c','user.name=t','-c','user.email=t@t','commit','--quiet','-m',text)
        return run('rev-parse','HEAD')

    def fake_build(self,mirror,sha,releases,python):
        release=releases/('0.3.0-'+sha[:7]);release.mkdir(parents=True);return release

    def run_update(self,ci='passed',health=True):
        return updater.update(self.args,ci=lambda repo,sha:ci,check=lambda url,timeout:health,restart_unit=self.restarts.append)

    def live(self):
        return (self.root/'current').resolve().name

    def test_first_run_adopts_hand_deployed_release(self):
        self.assertEqual(self.run_update(),'current');self.assertEqual(self.restarts,[])

    def test_server_commit_is_backed_up_switched_and_recorded(self):
        head=self.commit('server/axon_server/web/app.js','two')
        self.assertEqual(self.run_update(),'deployed')
        self.assertEqual(self.live(),'0.3.0-'+head[:7]);self.assertEqual(self.restarts,['axon-server'])
        self.assertEqual(len(list((self.state/'backups').glob('activity-before-'+head[:7]+'-*.sqlite3'))),1)
        self.assertEqual(json.loads((self.root/'update-state.json').read_text())['commit'],head)
        self.assertEqual(self.run_update(),'current')

    def test_commit_outside_the_server_restarts_nothing(self):
        self.commit('plugins/claude/README.md','docs')
        self.assertEqual(self.run_update(),'unchanged')
        self.assertEqual(self.live(),'0.3.0-'+self.first[:7]);self.assertEqual(self.restarts,[])

    def test_waits_for_ci_and_never_deploys_a_failed_commit(self):
        self.commit('server/axon_server/journal.py','two')
        self.assertEqual(self.run_update(ci='pending'),'pending')
        self.assertEqual(self.run_update(ci='failed'),'failed')
        self.assertEqual(self.run_update(ci='passed'),'current')
        self.assertEqual(self.live(),'0.3.0-'+self.first[:7]);self.assertEqual(self.restarts,[])

    def test_unhealthy_release_switches_back(self):
        head=self.commit('server/axon_server/journal.py','broken')
        self.assertEqual(self.run_update(health=False),'rolled back')
        self.assertEqual(self.live(),'0.3.0-'+self.first[:7]);self.assertEqual(self.restarts,['axon-server','axon-server'])
        self.assertFalse((self.root/'releases'/('0.3.0-'+head[:7])).exists())
        self.assertEqual(self.run_update(),'current')

    def test_prune_keeps_live_release_and_newest_backups(self):
        for index in range(7):
            (self.root/'releases'/f'0.3.0-old{index}').mkdir();(self.state/f'activity-before-old{index}-x.sqlite3').write_text('')
        (self.root/'staging-old').mkdir();(self.state/'backups').mkdir()
        updater.prune(self.root,3,self.state/'backups',self.state/'activity.sqlite3')
        self.assertTrue((self.root/'current').resolve().exists());self.assertFalse((self.root/'staging-old').exists())
        self.assertLessEqual(len(list((self.root/'releases').iterdir())),4)
        self.assertEqual(len(list(self.state.glob('activity-before-*'))),3)
        self.assertTrue((self.state/'activity.sqlite3').exists())

    def test_ci_state_reads_check_runs(self):
        class Response:
            def __init__(self,runs):self.body=json.dumps({'check_runs':runs}).encode()
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def read(self,*a):return self.body
        state=lambda runs:updater.ci_state('o/r','abc',opener=lambda request,timeout:Response(runs))
        self.assertEqual(state([]),'pending')
        self.assertEqual(state([{'status':'in_progress','conclusion':None}]),'pending')
        self.assertEqual(state([{'status':'completed','conclusion':'success'},{'status':'completed','conclusion':'skipped'}]),'passed')
        self.assertEqual(state([{'status':'completed','conclusion':'success'},{'status':'completed','conclusion':'failure'}]),'failed')

class RequestedRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.args=updater.parser().parse_args(['--root',str(self.root)]);self.now=1000.0;self.sleeps=[]

    def run_with(self,results,requested=True):
        if requested:(self.root/updater.REQUEST).touch();import os;os.utime(self.root/updater.REQUEST,(self.now,self.now))
        steps=iter(results)
        def step(args):
            result=next(steps)
            if isinstance(result,Exception):raise result
            return result
        def sleep(seconds):self.sleeps.append(seconds);self.now+=seconds
        return updater.run(self.args,step=step,sleep=sleep,clock=lambda:self.now)

    def test_timer_run_checks_once(self):
        self.assertEqual(self.run_with(['pending'],requested=False),'pending');self.assertEqual(self.sleeps,[])

    def test_requested_run_waits_for_ci_then_clears_the_request(self):
        self.assertEqual(self.run_with(['pending',OSError('rate limited'),'deployed']),'deployed')
        self.assertEqual(self.sleeps,[updater.REQUEST_INTERVAL]*2);self.assertFalse((self.root/updater.REQUEST).exists())

    def test_requested_run_gives_up_at_the_deadline(self):
        self.assertEqual(self.run_with(['pending']*100),'pending')
        self.assertLessEqual(len(self.sleeps)*updater.REQUEST_INTERVAL,updater.REQUEST_WAIT)
        self.assertFalse((self.root/updater.REQUEST).exists())

    def test_stale_request_is_a_plain_check(self):
        (self.root/updater.REQUEST).touch();import os;os.utime(self.root/updater.REQUEST,(0,0));self.now=updater.REQUEST_WAIT+1
        self.assertEqual(self.run_with(['pending'],requested=False),'pending');self.assertEqual(self.sleeps,[])
        self.assertFalse((self.root/updater.REQUEST).exists())

if __name__=='__main__':
    unittest.main()
