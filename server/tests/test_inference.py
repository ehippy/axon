import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import test_resources
from axon_reporter.inference import parse_metrics, collect, gpu_stats
from axon_protocol.resources import validate_resource
from axon_server.journal import Journal, inference_endpoints, poll_inference

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


class EndpointOptionTests(unittest.TestCase):
    def test_label_and_url_pairs(self):
        self.assertEqual(inference_endpoints(['neuralforge=http://neuralforge:13308/metrics',
            'moar=https://moar.example/metrics']), {'neuralforge':'http://neuralforge:13308/metrics','moar':'https://moar.example/metrics'})
    def test_rejects_unusable_options(self):
        for value in ('neuralforge','neuralforge=ftp://host/metrics','neuralforge=http://','bad label=http://h/metrics',
                      'way_too_long_label_'+'x'*80+'=http://h/metrics'):
            with self.assertRaises(ValueError): inference_endpoints([value])
    def test_rejects_duplicate_server_labels(self):
        with self.assertRaises(ValueError): inference_endpoints(['a=http://h1/metrics','a=http://h2/metrics'])


class PollerTests(unittest.TestCase):
    """The server pulls what the inference endpoint already exposes; nothing runs on that host."""
    EXPOSED=('# HELP llamacpp:requests_processing Requests in progress\n'
             'llamacpp:requests_processing 3\nllamacpp:requests_deferred 1\n'
             'llamacpp:predicted_tokens_seconds 44.5\nllamacpp:prompt_tokens_seconds 610.25\n'
             'llamacpp:kv_cache_usage_ratio 0.25\ngufo_device_lost_total 0\n')

    class Response:
        def __init__(self, body): self.body=body.encode()
        def __enter__(self): return self
        def __exit__(self,*exc): return False
        def read(self, limit=None): return self.body

    def poll(self, endpoints, **urlopen_kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            journal=Journal(Path(tmp)/'journal.db')
            stop=threading.Event()
            with patch('axon_server.journal.urlopen',**urlopen_kwargs):
                worker=threading.Thread(target=poll_inference,args=(journal,endpoints,stop,5),daemon=True); worker.start()
                rows=[]; deadline=time.monotonic()+8
                while time.monotonic()<deadline:
                    rows={row['account']:row for row in journal.resources() if row['provider']=='inference'}
                    if len(rows)==len(endpoints): break
                    time.sleep(.05)
                stop.set(); worker.join(timeout=3)
            return rows

    def test_records_what_the_endpoint_offers(self):
        rows=self.poll({'neuralforge':'http://neuralforge:13308/metrics'},return_value=PollerTests.Response(PollerTests.EXPOSED))
        row=rows['neuralforge']
        self.assertTrue(row['online'])
        self.assertEqual(row['metrics'],{'running':3,'queued':1,'generation_tps':44.5,'prompt_tps':610.25,'kv_cache_percent':25,'device_losses':0})
        self.assertEqual(row['gpus'],[])
        validate_resource(row)

    def test_multiple_servers_get_their_own_rows(self):
        rows=self.poll({'neuralforge':'http://neuralforge:13308/metrics','moar':'http://moar:13308/metrics'},
                       return_value=PollerTests.Response(PollerTests.EXPOSED))
        self.assertEqual(set(rows),{'neuralforge','moar'})
        self.assertTrue(all(row['online'] for row in rows.values()))

    def test_unreachable_endpoint_is_offline_with_no_invented_readings(self):
        rows=self.poll({'neuralforge':'http://neuralforge:13308/metrics'},side_effect=OSError)
        self.assertFalse(rows['neuralforge']['online']); self.assertEqual(rows['neuralforge']['metrics'],{})

    def test_something_other_than_metrics_is_offline(self):
        rows=self.poll({'neuralforge':'http://neuralforge:13308/metrics'},return_value=PollerTests.Response('<html>maintenance</html>'))
        self.assertFalse(rows['neuralforge']['online']); self.assertEqual(rows['neuralforge']['metrics'],{})

    def test_one_dead_server_does_not_stop_the_others(self):
        responses={'http://neuralforge:13308/metrics':PollerTests.Response(PollerTests.EXPOSED)}
        def urlopen(url,timeout=None):
            if url in responses: return responses[url]
            raise OSError('unreachable')
        rows=self.poll({'neuralforge':'http://neuralforge:13308/metrics','moar':'http://moar:13308/metrics'},side_effect=urlopen)
        self.assertTrue(rows['neuralforge']['online']); self.assertFalse(rows['moar']['online'])
