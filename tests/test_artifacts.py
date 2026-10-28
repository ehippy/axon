import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class ArtifactTests(unittest.TestCase):
    def test_built_hermes_plugin_loads_without_checkout_or_server(self):
        spec=importlib.util.spec_from_file_location('axon_test_builder',ROOT/'plugins/tools/manage.py')
        manager=importlib.util.module_from_spec(spec);spec.loader.exec_module(manager)
        with tempfile.TemporaryDirectory() as tmp:
            package=manager.build('hermes',Path(tmp)/'hermes')
            script = """import importlib.util, json, sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('standalone_hermes',Path(sys.argv[1])/'__init__.py')
plugin=importlib.util.module_from_spec(spec);spec.loader.exec_module(plugin)
result=plugin.before_turn(session_id='example-session',user_message='PRIVATE')
assert 'PRIVATE' not in result['context']
assert not any(name.startswith('axon_server') for name in sys.modules)
print(json.dumps({'loaded':True}))
"""
            env={k:v for k,v in os.environ.items() if k!='PYTHONPATH'}
            env.update(AXON_ACTIVITY_HOME=str(Path(tmp)/'state'),AXON_MOSQUITTO_PUB='/missing/axon-pub')
            result=subprocess.run([sys.executable,'-c',script,str(package)],cwd=tmp,env=env,capture_output=True,text=True,check=True)
            self.assertEqual(json.loads(result.stdout),{'loaded':True})

    def test_standalone_query_import_does_not_start_network_or_server(self):
        sys.path.insert(0,str(ROOT/'clients/query'))
        from axon_query import cli
        self.assertTrue(callable(cli.main))

if __name__=='__main__':unittest.main()
