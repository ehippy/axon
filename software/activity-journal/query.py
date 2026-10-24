#!/usr/bin/env python3
"""Read Axon's current sessions and activity history."""
import argparse
from datetime import datetime
import json
from urllib.parse import urlencode
from urllib.request import urlopen

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--url', default='http://127.0.0.1:8510')
p.add_argument('--json', action='store_true')
p.add_argument('view', choices=['sessions', 'events'])
for key in ('source', 'session', 'project', 'state', 'kind', 'q', 'after', 'limit'):
    p.add_argument('--' + key)
for key in ('since', 'until'):
    p.add_argument('--' + key, help='Unix timestamp or ISO date/time; local timezone if omitted')
args = p.parse_args()
filters = {k: v for k, v in vars(args).items() if k not in ('url', 'view', 'json') and v is not None}
for key in ('since', 'until'):
    if key in filters:
        try:
            filters[key] = float(filters[key])
        except ValueError:
            filters[key] = datetime.fromisoformat(filters[key]).timestamp()
with urlopen(args.url.rstrip('/') + '/' + args.view + '?' + urlencode(filters), timeout=5) as response:
    data = json.load(response)
if args.json:
    print(json.dumps(data, indent=2))
else:
    for row in data[args.view]:
        if args.view == 'sessions':
            print(f"{row['source']}/{row['session'][:8]} · {row.get('project', '?')} · {row['effective_state']}")
            for key in ('goal', 'step', 'activity', 'blocker', 'outcome', 'branch'):
                if row.get(key):
                    print(f"  {key}: {row[key]}")
            for evidence in row.get('evidence', []):
                print(f'  evidence: {evidence}')
        else:
            at = datetime.fromtimestamp(row['at']).astimezone().isoformat(timespec='seconds')
            print(f"{row['seq']} {at} {row['source']}/{row['session'][:8]} {row['kind']} {json.dumps(row['data'])}")
