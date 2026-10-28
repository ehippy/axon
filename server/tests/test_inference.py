import copy
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen as fetch
import test_resources
from axon_reporter.inference import parse_metrics, collect, gpu_stats
from axon_protocol.resources import validate_resource
from axon_server.journal import (GPU_HOLD, Journal, Unscraped, handler, held_gpus, inference_endpoints, inference_history,
    poll_inference, prometheus_metrics)

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

    def test_polling_does_not_erase_host_hardware_readings(self):
        """A poll that cannot see the GPU must blank it no more than the hold window allows."""
        with tempfile.TemporaryDirectory() as tmp:
            journal=Journal(Path(tmp)/'journal.db')
            gpu={'id':'card1','name':'AMD GPU','utilization_percent':64.0,'power_watts':80.0}
            journal.ingest_resource(dict(version=1,provider='inference',account='neuralforge',observed_at=time.time(),
                online=True,metrics={'running':2},gpus=[gpu]))
            self.assertEqual(held_gpus(journal,'neuralforge'),[gpu])
            with patch('axon_server.journal.urlopen',return_value=PollerTests.Response(PollerTests.EXPOSED)):
                stop=threading.Event(); worker=threading.Thread(target=poll_inference,args=(journal,{'neuralforge':'http://h/metrics'},stop,5),daemon=True)
                worker.start(); time.sleep(1.2); stop.set(); worker.join(timeout=3)
            row=next(r for r in journal.resources() if r['account']=='neuralforge')
            self.assertEqual(row['gpus'],[gpu],'host GPU readings survived a poll')
            self.assertEqual(row['metrics']['running'],3,'endpoint metrics still came from the poll')

    def test_stale_host_hardware_is_dropped_rather_than_shown_forever(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal=Journal(Path(tmp)/'journal.db')
            journal.ingest_resource(dict(version=1,provider='inference',account='neuralforge',observed_at=time.time()-GPU_HOLD-5,
                online=True,metrics={'running':1},gpus=[{'id':'card1','name':'AMD GPU','utilization_percent':64.0}]))
            self.assertEqual(held_gpus(journal,'neuralforge'),[])

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


class FakePrometheus:
    """Answers Prometheus API calls by the metric a query names; records every endpoint read."""
    def __init__(self, values=None, ranges=None, down=False, endpoint=PollerTests.EXPOSED):
        self.values, self.ranges, self.down, self.endpoint = values or {}, ranges or {}, down, endpoint
        self.queries, self.endpoint_reads = [], 0
    def __call__(self, url, timeout=None):
        parts = urlsplit(url)
        if not parts.path.startswith('/api/v1/'):
            self.endpoint_reads += 1
            if self.endpoint is None: raise OSError('unreachable')
            return PollerTests.Response(self.endpoint)
        if self.down: raise OSError('connection refused')
        params = {k: v[-1] for k, v in parse_qs(parts.query).items()}
        self.queries.append(params)
        table = self.ranges if parts.path.endswith('query_range') else self.values
        found = next((value for name, value in table.items() if name in params['query']), None)
        result = [] if found is None else [{'metric': {}, 'values' if table is self.ranges else 'value': found}]
        return PollerTests.Response(json.dumps({'status': 'success', 'data': {'resultType': 'vector', 'result': result}}))

LIVE = {'requests_processing': [0, '2'], 'requests_deferred': [0, '1'], 'tokens_predicted_total': [0, '38.5'],
        'prompt_tokens_total': [0, 'NaN'], 'kv_cache_usage_ratio': [0, '41.25'], 'gufo_device_lost_total': [0, '0']}


class PrometheusTests(unittest.TestCase):
    def test_live_load_comes_from_counter_rates(self):
        with patch('axon_server.journal.urlopen', FakePrometheus(LIVE)) as fake:
            metrics = prometheus_metrics('http://prom:9090', 'neuralforge')
        self.assertEqual(metrics, {'running': 2, 'queued': 1, 'generation_tps': 38.5, 'prompt_tps': 0.0,
                                   'kv_cache_percent': 41.25, 'device_losses': 0})
        self.assertTrue(all('instance="neuralforge"' in q['query'] for q in fake.queries))
        self.assertTrue(any('rate(llamacpp:tokens_predicted_total' in q['query'] for q in fake.queries),
                        'speed comes from counters, never the gauges a scrape resets')

    def test_a_server_prometheus_does_not_scrape_is_unscraped_not_idle(self):
        with patch('axon_server.journal.urlopen', FakePrometheus({})):
            with self.assertRaises(Unscraped): prometheus_metrics('http://prom:9090', 'moar')

    def poll(self, fake):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Journal(Path(tmp)/'journal.db'); stop = threading.Event()
            with patch('axon_server.journal.urlopen', fake):
                worker = threading.Thread(target=poll_inference, args=(journal, {'neuralforge': 'http://nf:13308/metrics'}, stop, 5, 4, 'http://prom:9090'), daemon=True)
                worker.start(); deadline = time.monotonic()+8
                while time.monotonic() < deadline and not any(r['provider'] == 'inference' for r in journal.resources()): time.sleep(.05)
                stop.set(); worker.join(timeout=3)
            return next(r for r in journal.resources() if r['provider'] == 'inference')

    def test_poller_leaves_the_endpoint_alone_while_prometheus_answers(self):
        fake = FakePrometheus(LIVE); row = self.poll(fake)
        self.assertTrue(row['online']); self.assertEqual(row['metrics']['generation_tps'], 38.5)
        self.assertEqual(fake.endpoint_reads, 0)

    def test_poller_asks_the_endpoint_when_prometheus_is_down(self):
        fake = FakePrometheus(down=True); row = self.poll(fake)
        self.assertTrue(row['online']); self.assertEqual(row['metrics']['generation_tps'], 44.5); self.assertEqual(fake.endpoint_reads, 1)

    def test_no_scrape_and_no_endpoint_is_offline(self):
        row = self.poll(FakePrometheus({}, endpoint=None))
        self.assertFalse(row['online']); self.assertEqual(row['metrics'], {})

    def test_history_steps_clamps_and_leaves_gaps(self):
        fake = FakePrometheus(ranges={'tokens_predicted_total': [[100, '12.5'], [130, 'NaN']], 'node_hwmon_power': [[100, '80']]})
        with patch('axon_server.journal.urlopen', fake):
            history = inference_history('http://prom:9090', 'neuralforge', 10**9, end=2_000_000_000)
        self.assertEqual(history['range'], 30*86400); self.assertEqual(history['step'], 30*86400//120)
        self.assertEqual(history['series'], {'output_tps': [[100.0, 12.5], [130.0, None]], 'gpu_power_watts': [[100.0, 80.0]]})
        self.assertEqual({q['step'] for q in fake.queries}, {str(history['step'])})
        with patch('axon_server.journal.urlopen', FakePrometheus()):
            self.assertEqual(inference_history('http://prom:9090', 'neuralforge', 1)['step'], 15)

    def serve(self, **options):
        journal = Journal(Path(self.tmp.name)/'journal.db')
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler(journal, **options))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join()))
        return f'http://127.0.0.1:{server.server_port}/resources/history'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)

    def status(self, url):
        try:
            with fetch(url) as response: return response.status, json.load(response)
        except HTTPError as error:
            with error: return error.code, None

    def test_history_endpoint(self):
        self.assertEqual(self.status(self.serve(inference={'neuralforge': 'http://nf/metrics'})+'?account=neuralforge')[0], 404,
                         'no Prometheus, no history')
        url = self.serve(prometheus_url='http://prom:9090', inference={'neuralforge': 'http://nf/metrics'})
        with patch('axon_server.journal.urlopen', FakePrometheus(ranges={'requests_processing': [[100, '1']]})):
            code, body = self.status(url+'?account=neuralforge&range=3600')
            self.assertEqual(code, 200); self.assertEqual(body['series'], {'running': [[100.0, 1.0]]})
            self.assertEqual(self.status(url+'?account=elsewhere')[0], 404, 'only configured servers are queried')
            self.assertEqual(self.status(url+'?account=neuralforge&range=soon')[0], 400)
        with patch('axon_server.journal.urlopen', FakePrometheus(down=True)):
            self.assertEqual(self.status(url+'?account=neuralforge')[0], 502)
