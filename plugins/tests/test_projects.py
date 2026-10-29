import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
for package in ('axon_reporter','axon_protocol'):
    sys.path.insert(0,str(ROOT/'packages/python'/package))
from axon_reporter import project, report


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.repo=self.root/'sample';self.repo.mkdir()
        self.git('init','-q');self.git('config','user.email','test@example.invalid');self.git('config','user.name','Test')
        self.git('commit','-qm','Initial','--allow-empty')
        env=patch.dict(os.environ,{'AXON_ACTIVITY_HOME':str(self.root/'state'),
            'AXON_CONFIG_FILE':str(self.root/'missing-env'),'AXON_MACHINE':'test-host'})
        env.start();self.addCleanup(env.stop)

    def git(self,*args):
        return subprocess.run(['git','-C',str(self.repo),*args],check=True,capture_output=True,text=True).stdout.strip()

    def test_origin_normalization_removes_transport_credentials_and_suffix(self):
        variants=['https://secret:password@github.com/team/repo.git','git@github.com:team/repo.git',
                  'ssh://git@github.com:22/team/repo.git','https://GITHUB.COM:443/team/repo/']
        self.assertEqual({project.normalized_origin(v) for v in variants},{'github.com/team/repo'})

    def test_worktrees_and_clones_share_identity_and_main_name(self):
        self.git('remote','add','origin','https://secret:password@example.invalid/team/sample.git')
        before=project.inspect(self.repo)
        tree=self.root/'parallel';self.git('worktree','add','-qb','parallel',str(tree))
        after=project.inspect(tree)
        self.assertEqual(before['public']['project_id'],after['public']['project_id'])
        self.assertEqual(after['public']['project'],'sample')
        self.assertEqual(after['public']['worktree'],'parallel')
        self.assertEqual(before['public']['worktree'],'')

    def test_root_commit_identity_matches_a_clone_without_remote(self):
        before=project.inspect(self.repo)
        clone=self.root/'clone'
        subprocess.run(['git','clone','-q',str(self.repo),str(clone)],check=True)
        subprocess.run(['git','-C',str(clone),'remote','remove','origin'],check=True)
        self.assertEqual(before['public']['project_id'],project.inspect(clone)['public']['project_id'])

    def test_bare_and_non_git_and_unborn(self):
        bare=self.root/'sample.git'
        subprocess.run(['git','clone','-q','--bare',str(self.repo),str(bare)],check=True)
        info=project.inspect(bare)
        self.assertTrue(info['bare']);self.assertEqual(info['public']['project'],'sample')
        self.assertIsNone(project.inspect(self.root))
        unborn=self.root/'unborn';unborn.mkdir();subprocess.run(['git','init','-q',str(unborn)],check=True)
        self.assertIsNone(project.inspect(unborn))

    def test_inventory_public_fields_exclude_paths_remotes_and_native_ids(self):
        remote='https://secret:password@example.invalid/team/sample.git'
        self.git('remote','add','origin',remote)
        public=project.remember(self.repo,'codex','private-native-thread-id')
        serialized=json.dumps({'public':public,'inventory':project.inventory()})
        for secret in (str(self.root),remote,'secret','password','private-native-thread-id'):
            self.assertNotIn(secret,serialized)
        self.assertEqual(project.registry_path().stat().st_mode & 0o777,0o600)
        with sqlite3.connect(project.registry_path()) as db:
            self.assertEqual(db.execute('SELECT native_id FROM native_sessions').fetchone()[0],'private-native-thread-id')

    def test_emitted_outbox_has_identity_but_no_paths_remote_or_native_id(self):
        remote='https://private-user:private-password@example.invalid/team/sample.git'
        self.git('remote','add','origin',remote)
        with patch.object(report,'server_fields',return_value=None):
            event=report.report('codex','private-native-id','start',{'project':'Investigate wake'},flush=False,cwd=self.repo)
        self.assertEqual(event['data']['project'],'sample')
        self.assertEqual(event['data']['topic'],'Investigate wake')
        with sqlite3.connect(report.spool_path()) as db:
            serialized=db.execute('SELECT payload FROM outbox').fetchone()[0]
        for secret in (str(self.root),remote,'private-user','private-password','private-native-id'):
            self.assertNotIn(secret,serialized)
        with patch.object(report,'server_fields',return_value=None):
            outside=report.report('hermes','chat','progress',{'project':'Conversation'},flush=False,cwd=self.root)
        self.assertEqual(outside['data']['project'],'')
        self.assertEqual(outside['data']['project_id'],'')
        self.assertEqual(outside['data']['topic'],'Conversation')
