"""Host credentials are reused and never expose the dashboard token to the host."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'packages/python/axon_reporter'))
sys.path.insert(0, str(ROOT/'packages/python/axon_protocol'))
from axon_reporter import credentials


class CredentialTests(unittest.TestCase):
    def test_enrollment_stores_once_and_reuses_private_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'config/credential'
            env = {'AXON_HOST_CREDENTIAL_FILE': str(path), 'AXON_BROKER': 'broker.example',
                   'AXON_MACHINE': 'host-a', 'AXON_CONFIG_FILE': str(Path(tmp)/'absent')}
            result = subprocess.CompletedProcess([],0,stdout=json.dumps({'machine':'host-a','credential':'c'*43}).encode())
            with patch.dict(os.environ,env,clear=True), patch.object(credentials.subprocess,'run',return_value=result) as run:
                self.assertEqual(credentials.enroll(),path)
                self.assertEqual(credentials.read_credential(),'c'*43)
                self.assertEqual(path.stat().st_mode & 0o777,0o600)
                self.assertEqual(credentials.enroll(),path)
                self.assertEqual(run.call_count,1)
                args=run.call_args.args[0]
                self.assertEqual(args[:5],['ssh','-o','BatchMode=yes','--','broker.example'])
                self.assertEqual(json.loads(run.call_args.kwargs['input']),{'machine':'host-a'})

    def test_bad_enrollment_does_not_create_a_credential(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'credential'
            with patch.dict(os.environ,{'AXON_HOST_CREDENTIAL_FILE':str(path),'AXON_BROKER':'broker.example','AXON_MACHINE':'host-a'},clear=True):
                for response in ({'machine':'other','credential':'c'*43},{'machine':'host-a','credential':'short'}):
                    with patch.object(credentials.subprocess,'run',return_value=subprocess.CompletedProcess([],0,stdout=json.dumps(response).encode())):
                        with self.assertRaises(ValueError):credentials.enroll()
                    self.assertFalse(path.exists())
