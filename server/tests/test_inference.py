import copy
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import test_resources
from axon_reporter.inference import parse_metrics, collect, gpu_stats
from axon_protocol.resources import validate_resource
from axon_server.journal import Journal

class InferenceTests(unittest.TestCase):
    def sample(self,name='neuralforge'):
        return dict(version=1,provider='inference',account=name,observed_at=time.time(),online=True,metrics={'running':2,'queued':1},gpus=[])
    def test_gufo_metrics_and_nonfinite(self):
        self.assertEqual(parse_metrics('# ignored\nllamacpp:requests_processing 2\nllamacpp:kv_cache_usage_ratio .25\nllamacpp:predicted_tokens_seconds NaN'),{'running':2,'kv_cache_percent':25})
        with self.assertRaises(ValueError):parse_metrics('unrelated 1')
    def test_multiple_servers_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'journal.db';journal=Journal(path)
            for name in ('neuralforge','moar'):journal.ingest_resource(self.sample(name))
            self.assertEqual({r['account'] for r in Journal(path).resources() if r['provider']=='inference'},{'neuralforge','moar'})
    def test_invalid_payloads(self):
        for key,value in [('account','bad/topic'),('online',1),('metrics',{'running':float('nan')}),('metrics',{'secret':1})]:
            sample=self.sample();sample[key]=value
            with self.assertRaises(ValueError):validate_resource(sample)
    def test_offline_retains_hardware(self):
        with patch('axon_reporter.inference.urlopen',side_effect=OSError),patch('axon_reporter.inference.gpu_stats',return_value=[{'id':'0','name':'AMD','utilization_percent':0}]):
            result=collect('neuralforge','http://localhost/metrics')
        self.assertFalse(result['online']);self.assertEqual(result['metrics'],{});self.assertEqual(len(result['gpus']),1)
    def test_a_card_without_power_sensor_keeps_known_readings(self):
        with tempfile.TemporaryDirectory() as tmp:
            device=Path(tmp)/'card1/device';hwmon=device/'hwmon/hwmon1';hwmon.mkdir(parents=True)
            (device/'vendor').write_text('0x1002');(device/'hwmon/hwmon2').mkdir()
            (hwmon/'temp1_input').write_text('53000');(device/'hwmon/hwmon2/temp1_input').write_text('')
            with patch('axon_reporter.inference.subprocess.run',side_effect=OSError):values=gpu_stats(Path(tmp))
            self.assertEqual(values[0]['temperature_c'],53.0)
            self.assertNotIn('power_watts',values[0])
    def test_amd_shared_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            device=Path(tmp)/'card1/device';device.mkdir(parents=True)
            (device/'vendor').write_text('0x1002');(device/'gpu_busy_percent').write_text('75');(device/'mem_info_gtt_used').write_text('96000000000')
            with patch('axon_reporter.inference.subprocess.run',side_effect=OSError):values=gpu_stats(Path(tmp))
            self.assertEqual(values[0]['shared_used_bytes'],96000000000);self.assertEqual(values[0]['utilization_percent'],75)
