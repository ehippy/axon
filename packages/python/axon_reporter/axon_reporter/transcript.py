"""Conversation text for the dashboard chat: the user's prompts and the agent's replies, nothing else.

Tool calls, tool results, thinking and injected context are parsed past and never kept. Text
that looks like a secret is masked on this host before it is sent; that is a best effort, so
the stream is opt-in per agent (AXON_TRANSCRIPT) and the server keeps it only briefly.
"""
from contextlib import closing
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import urllib.error
import urllib.request

from . import report
from .credentials import read_credential

AGENTS = {'claude'}
TEXT_LIMIT = 8000
BATCH = 200
MASK = '[redacted]'
# Harness wrappers around a prompt line: commands, notices and dashboard input already in the inbox.
WRAPPERS = ('<command-', '<local-command', '<task-notification', '<bash-', '<system-reminder', '<user-memory-input')

PATTERNS = [re.compile(p, f) for p, f in (
    (r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)', re.S),
    (r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b', 0),
    (r'\bgh[pousr]_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b', 0),
    (r'\bglpat-[A-Za-z0-9_-]{20,}\b', 0),
    (r'\bxox[abposr]-[A-Za-z0-9-]{10,}\b', 0),
    (r'\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b', 0),
    (r'\b[spr]k_live_[A-Za-z0-9]{20,}\b', 0),
    (r'\bAIza[0-9A-Za-z_-]{35}\b', 0),
    (r'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b', 0),
)]
BEARER = re.compile(r'(?i)\b(Bearer\s+)[A-Za-z0-9._~+/-]{12,}=*')
URL_PASSWORD = re.compile(r'(\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s@/]+(@)')
ASSIGNMENT = re.compile(r'(?i)(\b[\w.-]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential)[\w.-]*'
                        r'["\']?\s*[:=]\s*)(?:(["\'])[^"\'\n]{4,}?\2|[^\s"\'`,;)]{6,})')
LONG = re.compile(r'[A-Za-z0-9_+=-]{32,}')
UUID = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
SECRET_NAME = re.compile(r'(?i)token|secret|passw|api_?key|private|credential')


def enabled(agent):
    return agent in AGENTS and agent in re.split(r'[,\s]+', report.setting('AXON_TRANSCRIPT', '').strip())


def known_secrets():
    """Literal secret values this host holds: masked wherever they appear, whatever their shape."""
    values = {read_credential() or ''}
    for path in (report.setting('AXON_INPUT_TOKEN_FILE'), Path.home()/'.config/axon/input-token'):
        try:
            values.add(Path(path).expanduser().read_text().strip())
        except (OSError, TypeError):
            pass
    values.update(v for k, v in os.environ.items() if SECRET_NAME.search(k))
    return sorted((v for v in values if len(v) >= 8), key=len, reverse=True)


def random_looking(word):
    if UUID.fullmatch(word) or not (re.search(r'\d', word) and re.search(r'[A-Za-z]', word)):
        return False
    if re.fullmatch(r'[0-9a-f]+', word) and len(word) <= 40:
        return False  # commit hashes stay readable
    counts = {c: word.count(c) for c in set(word)}
    return -sum(n/len(word) * math.log2(n/len(word)) for n in counts.values()) >= 4.0


def redact(text, secrets=()):
    for value in secrets:
        text = text.replace(value, MASK)
    for pattern in PATTERNS:
        text = pattern.sub(MASK, text)
    text = BEARER.sub(lambda m: m.group(1)+MASK, text)
    text = URL_PASSWORD.sub(lambda m: m.group(1)+MASK+m.group(2), text)
    text = ASSIGNMENT.sub(lambda m: m.group(1)+(m.group(2) or '')+MASK+(m.group(2) or ''), text)
    return LONG.sub(lambda m: MASK if random_looking(m.group(0)) else m.group(0), text)


def timestamp(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def prompt_text(text):
    text = text.strip()
    return '' if not text or text.startswith(WRAPPERS) else text


def messages(line):
    """The conversation entries one transcript line holds, as (id, at, role, text)."""
    if line.get('isSidechain') or line.get('isMeta'):
        return []
    at, uid = timestamp(line.get('timestamp')), line.get('uuid')
    if at is None or not uid:
        return []
    kind, content = line.get('type'), (line.get('message') or {}).get('content')
    if kind == 'attachment':
        attachment = line.get('attachment') or {}
        # Typed while the agent was busy: it reaches the agent mid-turn as an attachment.
        text = prompt_text(attachment.get('prompt') or '') if attachment.get('type') == 'queued_command' and isinstance(attachment.get('prompt'), str) else ''
        return [(uid, at, 'user', text)] if text else []
    if kind == 'user':
        if isinstance(content, list):
            if any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in content):
                return []
            content = '\n\n'.join(b['text'] for b in content if isinstance(b, dict) and b.get('type') == 'text' and isinstance(b.get('text'), str))
        text = prompt_text(content) if isinstance(content, str) else ''
        return [(uid, at, 'user', text)] if text else []
    if kind == 'assistant' and isinstance(content, list):
        texts = [b['text'].strip() for b in content if isinstance(b, dict) and b.get('type') == 'text' and isinstance(b.get('text'), str)]
        return [(f'{uid}.{i}', at, 'assistant', text) for i, text in enumerate(texts) if text]
    return []


def scan(path, offset, limit=BATCH):
    """Conversation entries after offset, and the offset just past the last line read."""
    found = []
    with open(path, 'rb') as handle:
        handle.seek(offset)
        for raw in handle:
            if not raw.endswith(b'\n'):
                break  # a line still being written is read next time
            offset += len(raw)
            if b'"text"' not in raw and b'"user"' not in raw and b'queued_command' not in raw:
                continue  # most lines are tool output and bookkeeping: skip them unparsed
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if isinstance(line, dict):
                found.extend(messages(line))
            if len(found) >= limit:
                break
    return found, offset


def post(agent, session, entries):
    credential = read_credential()
    if not credential:
        return False
    source, key, _ = report.identity(agent, str(session))
    url = (report.setting('AXON_SERVER') or 'http://%s:8510' % report.setting('AXON_BROKER', 'localhost')).rstrip('/')
    body = json.dumps({'messages': [{'id': i, 'at': at, 'role': role, 'text': text} for i, at, role, text in entries]}).encode()
    request = urllib.request.Request(f'{url}/sessions/{source}/{key}/transcript', data=body,
                                     headers={'Authorization': 'Bearer '+credential, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=2):
            return True
    except (OSError, urllib.error.HTTPError):
        return False


def sync(agent, session, transcript):
    """Send new conversation text since the last sync; the cursor moves only once the server has it."""
    if not session or not transcript or not enabled(agent):
        return 0
    path = Path(transcript).expanduser()
    if path.suffix != '.jsonl' or not path.is_file():
        return 0
    source, key, _ = report.identity(agent, str(session))
    with closing(sqlite3.connect(report.spool_path(), timeout=.2)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS hook_transcript(source TEXT, session TEXT, path TEXT, offset INTEGER, PRIMARY KEY(source,session))')
        row = db.execute('SELECT path, offset FROM hook_transcript WHERE source=? AND session=?', (source, key)).fetchone()
    offset = row[1] if row and row[0] == str(path) and row[1] <= path.stat().st_size else 0
    found, end = scan(path, offset)
    if found:
        secrets = known_secrets()
        clean = [(i, at, role, redact(text, secrets)[:TEXT_LIMIT]) for i, at, role, text in found]
        if not post(agent, session, clean):
            return 0
    if end != offset:
        with closing(sqlite3.connect(report.spool_path(), timeout=.2)) as db, db:
            db.execute('INSERT OR REPLACE INTO hook_transcript VALUES(?,?,?,?)', (source, key, str(path), end))
    return len(found)
