"""Account quota snapshots: deliberately exclude credentials and session content."""
import math
import re
import time

PROVIDERS={'codex','claude'}

def number(value):
    return type(value) in (int,float) and math.isfinite(value)

def validate_resource(value):
    if isinstance(value,dict) and value.get('provider')=='inference':
        return validate_inference(value)
    if not isinstance(value,dict) or set(value)!={'version','provider','account','observed_at','windows'}:
        raise ValueError('invalid resource fields')
    if type(value['version']) is not int or value['version']!=1 or not isinstance(value['provider'],str) or value['provider'] not in PROVIDERS:
        raise ValueError('invalid resource provider')
    if not isinstance(value['account'],str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,80}',value['account']):
        raise ValueError('invalid resource account')
    if not number(value['observed_at']) or not 0<value['observed_at']<=time.time()+60:
        raise ValueError('invalid observation time')
    windows=value['windows']
    if not isinstance(windows,list) or len(windows)>2:
        raise ValueError('invalid resource windows')
    seen=set()
    for window in windows:
        if not isinstance(window,dict) or set(window)!={'name','window_minutes','used_percent','resets_at'}:
            raise ValueError('invalid window fields')
        if not isinstance(window['name'],str) or window['name'] not in ('short','weekly') or window['name'] in seen:
            raise ValueError('invalid window name')
        seen.add(window['name'])
        if window['used_percent'] is not None and (not number(window['used_percent']) or not 0<=window['used_percent']<=100):
            raise ValueError('invalid usage percentage')
        if window['resets_at'] is not None and (not number(window['resets_at']) or window['resets_at']<=0):
            raise ValueError('invalid reset time')
        if window['window_minutes'] is not None and (type(window['window_minutes']) is not int or not 0<window['window_minutes']<=527040):
            raise ValueError('invalid quota duration')
    return value


INFERENCE_METRICS={'running','queued','prompt_tps','generation_tps','kv_cache_percent','device_losses'}
GPU_METRICS={'utilization_percent','memory_used_bytes','memory_total_bytes','shared_used_bytes','shared_total_bytes','power_watts','temperature_c'}

def validate_inference(value):
    if set(value)!={'version','provider','account','observed_at','online','metrics','gpus'} or type(value['version']) is not int or value['version']!=1:
        raise ValueError('invalid inference fields')
    if not isinstance(value['account'],str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,80}',value['account']):
        raise ValueError('invalid inference identity')
    if not number(value['observed_at']) or not 0<value['observed_at']<=time.time()+60 or type(value['online']) is not bool:
        raise ValueError('invalid inference observation')
    def metrics(data, allowed):
        if not isinstance(data,dict) or not set(data)<=allowed:
            raise ValueError('invalid inference metrics')
        for key,v in data.items():
            if v is not None and (not number(v) or v<0 or (key.endswith('_percent') and v>100)):
                raise ValueError('invalid inference metric')
    metrics(value['metrics'],INFERENCE_METRICS)
    if not isinstance(value['gpus'],list) or len(value['gpus'])>32:
        raise ValueError('invalid GPUs')
    seen=set()
    for gpu in value['gpus']:
        if not isinstance(gpu,dict) or not {'id','name'}<=set(gpu) or not isinstance(gpu['id'],str) or not isinstance(gpu['name'],str) or len(gpu['name'])>160 or gpu['id'] in seen:
            raise ValueError('invalid GPU identity')
        seen.add(gpu['id']);metrics({k:v for k,v in gpu.items() if k not in ('id','name')},GPU_METRICS)
    return value
