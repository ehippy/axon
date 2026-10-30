import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
for project in ('packages/python/axon_protocol', 'packages/python/axon_reporter', 'server'):
    sys.path.insert(0, str(ROOT/project))
from axon_protocol import validate
from axon_server.journal import Journal
from axon_reporter import report


class ContractTests(unittest.TestCase):
    def fixture(self, name):
        return json.loads((ROOT/'protocol/fixtures'/f'{name}.json').read_text())

    def test_shared_fixtures_validate_and_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Journal(Path(tmp)/'journal.db', retention_days=0)
            for name in ('start', 'finished'):
                event = self.fixture(name)
                self.assertEqual(validate(event), event)
                journal.ingest(event)
            with self.assertRaises(ValueError):
                validate(self.fixture('invalid-finished'))
            expected = self.fixture('view')['sessions'][0]
            expected['wake'] = False
            expected['exited'] = False
            expected['work_state'] = 'finished'
            expected['work_at'] = self.fixture('finished')['at']
            # No repository is known by the old label, so it reads as a topic.
            expected['topic'] = expected.pop('project')
            self.assertEqual(journal.sessions(), [expected])

    def test_reporter_identity_is_stable_private_and_explicitly_overridable(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'AXON_ACTIVITY_HOME':tmp, 'HOME':tmp}, clear=True):
            first = report.identity('codex', 'chat')
            self.assertEqual(report.identity('codex', 'chat'), first)
            self.assertRegex(first[2], r'^[0-9a-f]{16}$')
            self.assertEqual(first[0], 'codex-'+first[2])
            self.assertNotEqual(report.identity('hermes', 'chat')[0], first[0])
            with patch.dict(os.environ, AXON_MACHINE='example-installation', AXON_SOURCE='example-source'):
                self.assertEqual(report.identity('codex', 'chat')[::2], ('example-source','example-installation'))
            with patch.dict(os.environ, AXON_SOURCE='bad/topic'):
                with self.assertRaises(ValueError): report.identity('codex', 'chat')

    def test_saved_settings_refresh_labels_without_changing_source(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'AXON_ACTIVITY_HOME':tmp, 'HOME':tmp}, clear=True):
            first=report.identity('codex','chat')
            config=Path(tmp)/'.config/axon/env';config.parent.mkdir(parents=True)
            config.write_text('export AXON_MACHINE=workstation\nexport AXON_BROKER=journal.example\nAXON_PORT=1884\n')
            second=report.identity('codex','chat')
            self.assertEqual(second[:2],first[:2])
            self.assertEqual(second[2],'workstation')
            self.assertEqual(report.setting('AXON_BROKER'),'journal.example')
            config.write_text('AXON_MACHINE=renamed\n')
            self.assertEqual(report.identity('codex','chat')[2],'renamed')
            with patch.dict(os.environ,AXON_MACHINE='override'):
                self.assertEqual(report.identity('codex','chat')[2],'override')

    def test_machine_kind_is_detected_overridable_and_validated(self):
        event=self.fixture('start')
        for kind in ('laptop','desktop','server'):
            validate({**event,'data':{**event['data'],'machine_kind':kind}})
        with self.assertRaises(ValueError):
            validate({**event,'data':{**event['data'],'machine_kind':'toaster'}})
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'AXON_ACTIVITY_HOME':tmp, 'HOME':tmp}, clear=True):
            with patch.object(report, 'detect_machine_kind', return_value='laptop'):
                self.assertEqual(report.machine_kind(), 'laptop')
                config=Path(tmp)/'.config/axon/env';config.parent.mkdir(parents=True)
                config.write_text('AXON_MACHINE_KIND=server\n')
                self.assertEqual(report.machine_kind(), 'server')
                config.write_text('AXON_MACHINE_KIND=toaster\n')
                with self.assertRaises(ValueError): report.machine_kind()
            with patch.object(report, 'detect_machine_kind', side_effect=OSError):
                config.unlink()
                self.assertIsNone(report.machine_kind())

    def test_auto_machine_label_uses_the_hostname_and_never_breaks_reporting(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'AXON_ACTIVITY_HOME':tmp, 'HOME':tmp, 'AXON_MACHINE':'auto'}, clear=True):
            with patch.object(report.socket, 'gethostname', return_value='Dev-Box.example'):
                source, session, machine = report.identity('codex', 'chat')
                self.assertEqual(machine, 'Dev-Box')
                # Only the host label changes; source identity stays the private install ID.
                self.assertEqual(source, 'codex-'+report.installation_identity())
                self.assertEqual(session, report.identity('codex', 'chat')[1])
            for alias in ('AUTO', ' auto '):
                with patch.dict(os.environ, AXON_MACHINE=alias), patch.object(report.socket, 'gethostname', return_value='Dev-Box'):
                    self.assertEqual(report.machine_identity(), 'Dev-Box')
            # An unusable or undeterminable host name falls back instead of failing.
            for broken in ('', '   ', '!!!', '.', '-', '@#$%'):
                with patch.object(report.socket, 'gethostname', return_value=broken):
                    self.assertEqual(report.machine_identity(), report.installation_identity())
            with patch.object(report.socket, 'gethostname', side_effect=OSError):
                self.assertEqual(report.machine_identity(), report.installation_identity())
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'AXON_ACTIVITY_HOME':tmp, 'HOME':tmp}, clear=True):
            # No label set at all still means a private random identity.
            self.assertRegex(report.machine_identity(), r'^[0-9a-f]{16}$')
            with patch.dict(os.environ, AXON_MACHINE='bad label!'):
                with self.assertRaisesRegex(ValueError, 'auto'):
                    report.machine_identity()

    def test_reporter_uses_existing_outbox_without_aggregate_publication(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, AXON_ACTIVITY_HOME=tmp, AXON_MACHINE='example'):
            db = Path(tmp)/'outbox.sqlite3'
            with sqlite3.connect(db) as connection:
                connection.execute('CREATE TABLE outbox(seq INTEGER PRIMARY KEY, topic TEXT, payload TEXT)')
                connection.execute('INSERT INTO outbox(topic,payload) VALUES(?,?)', ('old-topic', '{}'))
            report.report('codex','chat','start',{'goal':'Check packaging'},flush=False)
            with sqlite3.connect(db) as connection:
                topics=[row[0] for row in connection.execute('SELECT topic FROM outbox ORDER BY seq')]
            self.assertEqual(topics[0], 'old-topic')
            self.assertEqual(topics[1].split('/')[:2], ['axon','activity'])
            self.assertFalse(hasattr(report, 'publish_summary'))

if __name__ == '__main__': unittest.main()


class CompatibilityTests(unittest.TestCase):
    """Hosts and the server update separately, so either may be one release ahead (#22)."""
    def event(self, **data):
        return dict(version=1, source='claude-x', session='s', id='e1', at=__import__('time').time(), kind='heartbeat', data=data)

    def test_current_events_against_frozen_previous_release_validator(self):
        import importlib.util
        package=ROOT/'protocol/compat/previous'
        spec=importlib.util.spec_from_file_location('axon_previous_protocol',package/'__init__.py',
                                                   submodule_search_locations=[str(package)])
        previous=importlib.util.module_from_spec(spec);spec.loader.exec_module(previous)
        event=self.event(agent='codex',project='example',project_id='a'*64,worktree='parallel',topic='Work')
        self.assertEqual(previous.validate(event),validate(event))
        self.assertNotIn('project_id',previous.DATA_FIELDS)
        for name in ('start','finished'):
            old=json.loads((ROOT/'protocol/fixtures'/f'{name}.json').read_text())
            self.assertEqual(validate(previous.validate(old)),old)

    def test_newer_reporter_fields_are_kept_out_of_session_state(self):
        event = self.event(agent='claude', step='x', future_field={'n': 1})
        self.assertEqual(validate(event), event)
        with tempfile.TemporaryDirectory() as tmp:
            journal = Journal(Path(tmp)/'journal.db', retention_days=0)
            self.assertTrue(journal.ingest(event))
            self.assertNotIn('future_field', journal.sessions()[0])
            self.assertEqual(journal.events()[0]['data']['future_field'], {'n': 1})

    def test_unknown_fields_stay_small_and_plainly_named(self):
        for bad in ({'Bad-Key': 1}, {'blob': 'x'*3000}, {'n': float('nan')}):
            with self.assertRaises(ValueError): validate(self.event(**bad))
        with self.assertRaises(ValueError): validate(self.event(step=5))  # known fields stay strict

    def test_previous_release_fixtures_still_validate(self):
        for name in ('start', 'finished'):
            event = json.loads((ROOT/'protocol/fixtures'/f'{name}.json').read_text())
            self.assertEqual(validate(event), event)

    def test_resources_tolerate_new_readings(self):
        from axon_protocol.resources import validate_resource
        quota = dict(version=1, provider='codex', account='primary', observed_at=__import__('time').time(), windows=[], plan='pro')
        self.assertNotIn('plan', validate_resource(quota))
        inference = dict(version=1, provider='inference', account='nf', observed_at=__import__('time').time(), online=True,
                         metrics={'running': 1, 'new_metric': 3}, gpus=[{'id': 'g', 'name': 'GPU', 'utilization_percent': 5, 'fan_rpm': 900}], region='lab')
        value = validate_resource(inference)
        self.assertEqual(value['metrics'], {'running': 1}); self.assertEqual(value['gpus'][0], {'id': 'g', 'name': 'GPU', 'utilization_percent': 5})
        self.assertNotIn('region', value)
        with self.assertRaises(ValueError):
            validate_resource({**inference, 'metrics': {'new_metric': 'text'*1000}})

    def test_reporter_leaves_out_fields_the_server_does_not_know(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'AXON_ACTIVITY_HOME': tmp, 'HOME': tmp}, clear=True):
            with patch.object(report, 'server_fields', return_value={'agent', 'machine', 'step'}), patch.object(report, 'drain'):
                event = report.report('claude', 'abc', 'progress', {'step': 'x', 'tokens': {'input': 1, 'cached': 0, 'output': 1}})
            self.assertEqual(set(event['data']), {'agent', 'machine', 'step'})
            with patch.object(report, 'server_fields', return_value=None), patch.object(report, 'drain'):
                event = report.report('claude', 'abc', 'progress', {'step': 'x', 'tokens': {'input': 1, 'cached': 0, 'output': 1}})
            self.assertIn('tokens', event['data'])

    def test_server_advertises_what_it_understands(self):
        from axon_protocol import capabilities, DATA_FIELDS
        self.assertEqual(set(capabilities()['data_fields']), DATA_FIELDS)
