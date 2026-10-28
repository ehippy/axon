"""Cumulative token counts for one agent session, read from its own transcript.

Only usage numbers are taken from each line; prompts, replies and tool output are
parsed past and never kept. Reads resume where the last one stopped, so a long
session costs only its new lines per hook.
"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from . import report

FIELDS = ('input', 'cached', 'output')
LIMIT = 10**13


def claude_usage(usage):
    """Claude bills fresh, cache-write and cache-read input separately; all three were sent up."""
    fresh, written, read = (int(usage.get(k) or 0) for k in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens'))
    return {'input': fresh+written+read, 'cached': read, 'output': int(usage.get('output_tokens') or 0)}


def codex_usage(total):
    """Codex already reports session totals; its input count includes the cached share."""
    return {'input': int(total.get('input_tokens') or 0), 'cached': int(total.get('cached_input_tokens') or 0),
            'output': int(total.get('output_tokens') or 0)}


def scan(agent, path, state):
    """Advance state over new complete lines. Claude repeats one message's usage on each of its
    content lines, so the newest line for the current message replaces its earlier count."""
    with open(path, 'rb') as handle:
        handle.seek(state['offset'])
        for raw in handle:
            if not raw.endswith(b'\n'):
                break  # a line still being written is read next time
            state['offset'] += len(raw)
            if b'usage' not in raw and b'token_count' not in raw:
                continue
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if agent == 'codex':
                payload = line.get('payload') or {}
                total = (payload.get('info') or {}).get('total_token_usage') if payload.get('type') == 'token_count' else None
                if isinstance(total, dict):
                    state['totals'] = codex_usage(total)
            elif line.get('type') == 'assistant':
                message = line.get('message') or {}
                usage = message.get('usage')
                if not isinstance(usage, dict):
                    continue
                counted = claude_usage(usage)
                if message.get('id') and message.get('id') == state.get('last_id'):
                    previous = state['last']
                else:
                    previous = dict.fromkeys(FIELDS, 0)
                state['totals'] = {k: state['totals'][k]-previous[k]+counted[k] for k in FIELDS}
                state['last_id'], state['last'] = message.get('id'), counted
    return state


def session_tokens(agent, session, transcript):
    """Return {input, cached, output} so far, or None when the transcript cannot be read."""
    if agent not in ('claude', 'codex') or not transcript:
        return None
    path = Path(transcript).expanduser()
    if path.suffix != '.jsonl' or not path.is_file():
        return None
    try:
        with closing(sqlite3.connect(report.spool_path(), timeout=.2)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS hook_tokens(source TEXT, session TEXT, path TEXT, state TEXT, PRIMARY KEY(source,session))')
            row = db.execute('SELECT path, state FROM hook_tokens WHERE source=? AND session=?', (agent, session)).fetchone()
            state = json.loads(row[1]) if row and row[0] == str(path) else None
            if not state or state['offset'] > path.stat().st_size:
                state = {'offset': 0, 'totals': dict.fromkeys(FIELDS, 0), 'last_id': None, 'last': dict.fromkeys(FIELDS, 0)}
            state = scan(agent, path, state)
            db.execute('INSERT OR REPLACE INTO hook_tokens VALUES(?,?,?,?)', (agent, session, str(path), json.dumps(state)))
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return None
    totals = {k: max(0, min(LIMIT-1, int(state['totals'][k]))) for k in FIELDS}
    return totals if any(totals.values()) else None
