"""Host-local inference and GPU snapshots, one stable identity per server."""
import argparse
import csv
import json
from pathlib import Path
import math
import subprocess
import sys
import time
from urllib.request import urlopen
from axon_protocol.resources import validate_resource
from .resources import publish

NAMES={'llamacpp:requests_processing':'running','llamacpp:requests_deferred':'queued',
       'llamacpp:prompt_tokens_seconds':'prompt_tps','llamacpp:predicted_tokens_seconds':'generation_tps',
       'llamacpp:kv_cache_usage_ratio':'kv_cache_percent','gufo_device_lost_total':'device_losses'}

def parse_metrics(text):
    result={}
    for line in text.splitlines():
        parts=line.split()
        if len(parts)<2 or parts[0] not in NAMES:continue
        try:v=float(parts[1])
        except ValueError:continue
        if math.isfinite(v) and v>=0:result[NAMES[parts[0]]]=v*(100 if NAMES[parts[0]]=='kv_cache_percent' else 1)
    if not result:raise ValueError('No supported inference metrics')
    return result

def read_number(path, scale=1):
    try:return float(path.read_text().strip())/scale
    except (OSError,ValueError):return None

def gpu_stats(root=Path('/sys/class/drm')):
    gpus=[]
    for card in sorted(root.glob('card[0-9]*')):
        if not card.name[4:].isdigit():continue
        device=card/'device'
        try:vendor=(device/'vendor').read_text().strip()
        except OSError:continue
        if vendor!='0x1002':continue
        gpu={'id':card.name,'name':'AMD GPU'}
        for key,file in [('utilization_percent','gpu_busy_percent'),('memory_used_bytes','mem_info_vram_used'),('memory_total_bytes','mem_info_vram_total'),('shared_used_bytes','mem_info_gtt_used'),('shared_total_bytes','mem_info_gtt_total')]:
            gpu[key]=read_number(device/file)
        for hw in sorted((device/'hwmon').glob('hwmon*')):
            for key,file,scale in (('temperature_c','temp1_input',1000),('power_watts','power1_average',1000000)):
                value=read_number(hw/file,scale)
                if value is not None:gpu[key]=value
        gpus.append(gpu)
    try:
        proc=subprocess.run(['nvidia-smi','--query-gpu=uuid,name,utilization.gpu,memory.used,memory.total,power.draw,temperature.gpu','--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=3,check=True)
        for row in csv.reader(proc.stdout.splitlines()):
            if len(row)!=7:continue
            gpu={'id':row[0].strip(),'name':row[1].strip()}
            for key,v,scale in zip(('utilization_percent','memory_used_bytes','memory_total_bytes','power_watts','temperature_c'),row[2:],(1,1048576,1048576,1,1)):
                try:gpu[key]=float(v)*scale
                except ValueError:gpu[key]=None
            gpus.append(gpu)
    except (OSError,subprocess.SubprocessError):pass
    return gpus

def collect(server,url):
    online=False;metrics={}
    try:
        with urlopen(url,timeout=4) as response:metrics=parse_metrics(response.read(1048576).decode())
        online=True
    except (OSError,ValueError):pass
    return validate_resource(dict(version=1,provider='inference',account=server,observed_at=time.time(),online=online,metrics=metrics,gpus=gpu_stats()))

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server',required=True,help='Stable server label, e.g. neuralforge or moar')
    parser.add_argument('--metrics-url',default='http://127.0.0.1:13308/metrics')
    parser.add_argument('--interval',type=float,default=10)
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--print',action='store_true',dest='print_only')
    args=parser.parse_args()
    if not math.isfinite(args.interval) or args.interval<5:parser.error('interval must be at least 5 seconds')
    while True:
        try:
            value=collect(args.server,args.metrics_url)
            if args.print_only:print(json.dumps(value),flush=True)
            else:publish(value)
        except (OSError,ValueError) as exc:print(str(exc),file=sys.stderr)
        if args.once:return
        time.sleep(args.interval)

if __name__=='__main__':main()
