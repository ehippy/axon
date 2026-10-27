"""Shared, fail-open integration runtime for Axon agent plugins."""
from contextlib import closing
import json
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import time

from . import report


def context(agent, session):
    if not session:
        return ''
    command = shlex.join([sys.executable, str(Path(report.__file__).resolve()), '--agent', agent, '--session', str(session)])
    return (
        'Axon activity reporting is enabled for this session. Report a short semantic goal before substantive work, '
        'then significant milestones, direction changes, or blockers. Use your existing terminal tool with this command prefix: '
        + command + '. Append: start --project PROJECT --goal "Desired outcome" --step "Current step"; '
        'progress --step "Meaningful progress"; waiting --step "Decision needed"; blocked --blocker "Dependency"; '
        'or finished --outcome "Concrete result" --evidence "Actual commit, test, PR, or artifact reference". '
        'Report finished only when the requested work is complete, before your final response. '
        'For a conversation without substantive work, lifecycle status is enough. '
        'Use progress to refine the goal during the same task, and start --goal when beginning or resuming work. '
        'Hooks already track lifecycle and generic tools; do not report each tool call. '
        'Do not include credentials, raw prompts, transcripts, command arguments, or unverified success claims. '
        'A reporting failure must not block the task; reports queue locally for retry. '
        'Do not invent a session ID or use another chat\'s reporting command.'
    )


def branch(cwd):
    if not cwd:
        return ''
    try:
        p = subprocess.run(['git', '-C', cwd, 'branch', '--show-current'], capture_output=True, text=True, timeout=.25)
        return p.stdout.strip()[:200] if p.returncode == 0 else ''
    except (OSError, subprocess.TimeoutExpired):
        return ''


def heartbeat_due(agent, session, now=None):
    """Limit tool chatter to one heartbeat per 30s per chat; lifecycle never throttled."""
    now = time.time() if now is None else now
    source, key, _ = report.identity(agent, str(session))
    with closing(sqlite3.connect(report.spool_path(), timeout=.2)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS hook_heartbeats(source TEXT, session TEXT, at REAL, PRIMARY KEY(source,session))')
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT at FROM hook_heartbeats WHERE source=? AND session=?', (source,key)).fetchone()
        if row and 0 <= now - row[0] < 30:
            return False
        db.execute('INSERT OR REPLACE INTO hook_heartbeats VALUES(?,?,?)', (source,key,now))
        return True


def lifecycle(agent, data):
    """Only identifiers, cwd, event and tool names are read; other payloads are ignored."""
    session = data.get('session_id')
    event = data.get('hook_event_name')
    if not session:
        return
    safe = {key: data[key] for key in ('session_id','cwd','hook_event_name','tool_name') if key in data}
    if event in ('PreToolUse','PostToolUse') and not heartbeat_due(agent,session):
        return
    if event == 'UserPromptSubmit':
        # A new user turn may continue the same task. Do not erase its semantic goal.
        patch = {'state':'working', 'activity':'working'}
        if safe.get('cwd'):
            patch['project'] = Path(safe['cwd']).name[:120]
            current_branch = branch(safe['cwd'])
            if current_branch:
                patch['branch'] = current_branch
        parent = data.get('parent_session_id')
        if parent:
            source, hashed, _ = report.identity(agent,str(parent))
            patch['parent'] = f'{source}/{hashed}'
        report.report(agent,str(session),'progress',patch)
    else:
        report.hook(safe,agent)


def run_hook(agent):
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            return
        # Context is still provided if the broker or local outbox is unavailable.
        try:
            lifecycle(agent,data)
        except Exception:
            pass
        if data.get('hook_event_name') in ('SessionStart','UserPromptSubmit') and data.get('session_id'):
            print(json.dumps({'hookSpecificOutput':{'hookEventName':data['hook_event_name'],
                  'additionalContext':context(agent,data['session_id'])}}))
    except Exception:
        pass  # no decisions, stderr, or nonzero exits from a reporting hook
