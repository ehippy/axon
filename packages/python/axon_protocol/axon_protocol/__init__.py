"""Axon activity protocol v1: validation without server or transport dependencies."""
import re
import time

PROTOCOL_VERSION = 1

STATES = {'working', 'waiting', 'blocked', 'finished', 'stopped', 'interrupted'}
FIELDS = {'agent': 80, 'machine': 80, 'project': 120, 'goal': 400, 'step': 400,
          'activity': 120, 'outcome': 1000, 'parent': 160, 'branch': 200, 'blocker': 400}
TERMINAL = {'finished', 'stopped', 'interrupted'}


def validate(v):
    if not isinstance(v, dict) or v.get('version') != 1:
        raise ValueError('version must be 1')
    for key in ('source', 'session', 'id'):
        if not isinstance(v.get(key), str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,128}', v[key]):
            raise ValueError('invalid ' + key)
    if v.get('kind') not in {'start', 'progress', 'heartbeat', 'waiting', 'blocked', 'finished', 'stopped', 'interrupted'}:
        raise ValueError('invalid kind')
    at = v.get('at')
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not 0 < at <= time.time() + 60:
        raise ValueError('invalid timestamp')
    patch = v.get('data', {})
    if not isinstance(patch, dict) or set(patch) - (set(FIELDS) | {'state', 'evidence', 'lease_seconds'}):
        raise ValueError('invalid data fields')
    for key, limit in FIELDS.items():
        if key in patch and (not isinstance(patch[key], str) or len(patch[key]) > limit):
            raise ValueError('invalid ' + key)
    if 'state' in patch and patch['state'] not in STATES:
        raise ValueError('invalid state')
    evidence = patch.get('evidence', [])
    if not isinstance(evidence, list) or len(evidence) > 12 or any(not isinstance(x, str) or not x.strip() or len(x) > 500 for x in evidence):
        raise ValueError('invalid evidence')
    lease = patch.get('lease_seconds', 900)
    if type(lease) is not int or not 30 <= lease <= 3600:
        raise ValueError('invalid lease')
    if v['kind']=='finished' or patch.get('state')=='finished':
        if not patch.get('outcome','').strip() or not evidence:
            raise ValueError('finished requires outcome and evidence')
    return v
