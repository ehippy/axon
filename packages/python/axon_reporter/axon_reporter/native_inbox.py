"""Private long-poll bridge for native plugins. Never emits messages as activity.

stdin: session identity line, then one {accepted: bool} line per emitted batch.
stdout: leased {messages: [...]} batches. The owning plugin calls its native API
before acknowledging. Closing a plugin must terminate this child process.
"""
from contextlib import closing
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from urllib.error import HTTPError
import urllib.request

if not __package__:
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    protocol=Path(__file__).resolve().parents[2]/'axon_protocol'
    if protocol.is_dir():sys.path.insert(0,str(protocol))

from axon_reporter import report
from axon_reporter.plugin_runtime import claim,acknowledge_wake,server_url,wake_enabled

PATHS={'pi':'pi-native','hermes':'hermes-native','opencode':'opencode-native'}


def enabled(agent):
    return wake_enabled(agent)


def server_ready():
    with urllib.request.urlopen(server_url().rstrip('/')+'/health',timeout=3) as response:
        settings=json.load(response).get('host_claims',{})
    return settings.get('required') is True and settings.get('wake_leases') is True


def accepted_ids(agent, session, ids, remember=False):
    """A local receipt prevents reinjection when a server acknowledgment is retried.

    Only message IDs and hashed session routing enter this table; message text,
    credentials, and native IDs do not.
    """
    source,key,_=report.identity(agent,str(session))
    path=report.spool_path()
    fd=os.open(path,os.O_CREAT|os.O_WRONLY,0o600);os.close(fd)
    with closing(sqlite3.connect(path,timeout=.5)) as db,db:
        db.execute('CREATE TABLE IF NOT EXISTS native_deliveries(source TEXT,session TEXT,message_id TEXT,at REAL,PRIMARY KEY(source,session,message_id))')
        if remember:
            db.executemany('INSERT OR IGNORE INTO native_deliveries VALUES(?,?,?,?)',
                [(source,key,message_id,time.time()) for message_id in ids])
        return {row[0] for row in db.execute('SELECT message_id FROM native_deliveries WHERE source=? AND session=?',(source,key))} & set(ids)


def main(agent):
    config=json.loads(sys.stdin.readline(16384))
    if not isinstance(config,dict):return
    session=config.get('session_id')
    if not isinstance(session,str) or not session or agent not in PATHS or not enabled(agent):
        return
    while True:
        try:
            if not server_ready():return
            break
        except OSError:
            time.sleep(2)
    while True:
        try:
            batch=claim(agent,session,wait=50,wake_path=PATHS[agent])
            if not batch:
                time.sleep(1)
                continue
            # Never inject an unleased response from an incompatible server.
            if any(not isinstance(m.get('claim_token'),str) for m in batch):return
            known=accepted_ids(agent,session,[m['id'] for m in batch])
            already=[m for m in batch if m['id'] in known]
            if already:acknowledge_wake(agent,session,already)
            pending=[m for m in batch if m['id'] not in known]
            if not pending:continue
            print(json.dumps({'messages':pending}),flush=True)
            line=sys.stdin.readline(16384)
            if not line:return
            reply=json.loads(line)
            accepted=reply.get('accepted') is True
            if accepted:accepted_ids(agent,session,[m['id'] for m in pending],remember=True)
            acknowledge_wake(agent,session,pending,accepted)
            if not accepted:time.sleep(2)
        except HTTPError as error:
            # The first lifecycle event can still be crossing MQTT when the HTTP
            # poll starts. Retry authorization without injecting anything until
            # that event establishes ownership (or a revoked credential is repaired).
            if error.code in (403,404,501):return
            time.sleep(2)
        except (OSError,ValueError,sqlite3.Error):
            time.sleep(2)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent',choices=PATHS,required=True)
    try:main(parser.parse_args().agent)
    except (OSError,ValueError):pass
