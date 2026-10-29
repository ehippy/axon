from pathlib import Path
import shutil
import subprocess
import unittest


class PiNativeTests(unittest.TestCase):
    def test_mocked_native_context_and_bridge_lifecycle(self):
        node=shutil.which('node')
        self.assertIsNotNone(node,'Native plugin tests require Node 24+')
        result=subprocess.run([node,'--experimental-strip-types','--test',str(Path(__file__).with_name('native.test.mjs'))],
                              capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
