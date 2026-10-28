"""Collect documented provider quotas; publish only normalized account snapshots."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import select
import shutil
import sqlite3
import subprocess
import sys
import time

from axon_protocol.resources import validate_resource
from . import report


def snapshot(provider, windows, now=None):
    account=report.setting('AXON_'+provider.upper()+'_ACCOUNT') or report.setting('AXON_RESOURCE_ACCOUNT','primary')
    return validate_resource(dict(version=1,provider=provider,account=account,
        observed_at=time.time() if now is None else now,windows=windows))


def codex_snapshot(result, now=None):
    limits=(result.get('rateLimitsByLimitId') or {}).get('codex') or result.get('rateLimits') or {}
    windows=[]
    for key,name in (('primary','short'),('secondary','weekly')):
        value=limits.get(key)
        if isinstance(value,dict):
            windows.append(dict(name=name,window_minutes=value.get('windowDurationMins'),
                used_percent=value.get('usedPercent'),resets_at=value.get('resetsAt')))
    if not windows:raise ValueError('Codex quota data unavailable')
    return snapshot('codex',windows,now)


def claude_snapshot(data, now=None):
    rates=data.get('rate_limits') or {};windows=[]
    for key,name,minutes in (('five_hour','short',300),('seven_day','weekly',10080)):
        value=rates.get(key)
        if isinstance(value,dict):
            windows.append(dict(name=name,window_minutes=minutes,
                used_percent=value.get('used_percentage'),resets_at=value.get('resets_at')))
    if not windows:return None
    return snapshot('claude',windows,now)


def publish(value):
    validate_resource(value)
    path=report.spool_path();fd=os.open(path,os.O_CREAT|os.O_WRONLY,0o600);os.close(fd)
    signature=json.dumps(value['windows'],sort_keys=True)
    queued=False
    with closing(sqlite3.connect(path,timeout=.2)) as db,db:
        db.execute('CREATE TABLE IF NOT EXISTS outbox(seq INTEGER PRIMARY KEY,topic TEXT,payload TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS resource_samples(provider TEXT,account TEXT,signature TEXT,sent_at REAL,PRIMARY KEY(provider,account))')
        db.execute('BEGIN IMMEDIATE')
        prior=db.execute('SELECT signature,sent_at FROM resource_samples WHERE provider=? AND account=?',(value['provider'],value['account'])).fetchone()
        if not prior or prior[0]!=signature or value['observed_at']-prior[1]>=60:
            db.execute('INSERT INTO outbox(topic,payload) VALUES(?,?)',
                ('axon/resources/'+value['provider']+'/'+value['account'],json.dumps(value)))
            db.execute('INSERT OR REPLACE INTO resource_samples VALUES(?,?,?,?)',
                (value['provider'],value['account'],signature,value['observed_at']))
            queued=True
    report.drain(path)
    return queued


def read_codex(binary=None):
    binary=binary or report.setting('AXON_CODEX_BIN') or shutil.which('codex') or str(Path.home()/'.local/bin/codex')
    proc=subprocess.Popen([binary,'app-server','--listen','stdio://'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL,bufsize=0,cwd=Path.home())
    pending=b''
    def rpc(identifier,method,params=None):
        nonlocal pending
        request={'id':identifier,'method':method}
        if params is not None:request['params']=params
        proc.stdin.write((json.dumps(request)+'\n').encode());proc.stdin.flush()
        deadline=time.monotonic()+12
        while time.monotonic()<deadline:
            while b'\n' in pending:
                line,pending=pending.split(b'\n',1)
                result=json.loads(line)
                if result.get('id')==identifier:
                    if 'error' in result:raise ValueError('Codex quota request failed')
                    return result['result']
            if not select.select([proc.stdout],[],[],max(0,deadline-time.monotonic()))[0]:break
            chunk=os.read(proc.stdout.fileno(),65536)
            if not chunk:raise ValueError('Codex app server exited')
            pending+=chunk
            if len(pending)>1048576:raise ValueError('Unexpected app-server response size')
        raise TimeoutError('Codex quota request timed out')
    try:
        rpc(1,'initialize',{'clientInfo':{'name':'axon-resources','version':'0.3.0'}})
        proc.stdin.write(b'{"method":"initialized"}\n');proc.stdin.flush()
        return codex_snapshot(rpc(2,'account/rateLimits/read'))
    finally:
        if proc.poll() is None:proc.terminate()
        try:proc.wait(timeout=3)
        except subprocess.TimeoutExpired:proc.kill();proc.wait()
        proc.stdin.close();proc.stdout.close()


def collect_claude(raw):
    """Publish Claude's status-line rate limits; never raise into the host agent."""
    try:
        value=claude_snapshot(json.loads(raw))
        if value:publish(value)
        return value
    except (ValueError,TypeError,AttributeError,OSError,sqlite3.Error):return None


def statusline(raw, display_command=None):
    value=collect_claude(raw)
    if display_command:
        try:
            result=subprocess.run(['/bin/sh','-c',display_command],input=raw,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=2)
            sys.stdout.buffer.write(result.stdout);return
        except (OSError,subprocess.TimeoutExpired):pass
    parts=['Claude']
    for window in value['windows'] if value else []:
        if window['used_percent'] is not None:
            parts.append(('5h' if window['name']=='short' else 'Weekly')+': '+str(round(100-window['used_percent']))+'% left')
    print(' · '.join(parts))


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('codex')
    claude=sub.add_parser('claude-statusline');claude.add_argument('--display-command')
    args=parser.parse_args()
    if args.command=='claude-statusline':
        statusline(sys.stdin.buffer.read(),args.display_command);return
    try:
        publish(read_codex())
        print('Codex resource snapshot queued')
    except (OSError,ValueError,TimeoutError,sqlite3.Error):
        report.drain();print('Codex resource data unavailable',file=sys.stderr);sys.exit(1)

if __name__=='__main__':main()
