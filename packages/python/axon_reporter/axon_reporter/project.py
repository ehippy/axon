"""Git project identities and private checkout/session inventory.

Only `public` metadata may be emitted. Paths, remotes and native session IDs
are stored separately on the host for the job runner.
"""
from contextlib import closing
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from urllib.parse import urlsplit


def git(cwd, *args):
    result = subprocess.run(['git', '-C', str(cwd), *args], capture_output=True,
                            text=True, timeout=2)
    return result.stdout.strip() if result.returncode == 0 else ''


def normalized_origin(value):
    value = value.strip().rstrip('/')
    if '://' in value:
        url = urlsplit(value)
        host = (url.hostname or '').lower()
        port = url.port
        if port and (url.scheme, port) not in (('ssh', 22), ('https', 443), ('http', 80)):
            host += ':'+str(port)
        value = host+'/'+url.path.lstrip('/')
    elif (match := re.fullmatch(r'(?:[^/@:]+@)?([^/:]+):(.+)', value)):
        value = match[1].lower()+'/'+match[2].lstrip('/')
    if value.endswith('.git'):
        value = value[:-4]
    return value.rstrip('/')


def inspect(cwd):
    """Return public metadata plus host-only checkout details, or None outside git."""
    if not cwd:
        return None
    try:
        common = git(cwd, 'rev-parse', '--path-format=absolute', '--git-common-dir')
        if not common:
            return None
        common = Path(common).resolve()
        bare = git(cwd, 'rev-parse', '--is-bare-repository') == 'true'
        main = common if bare else common.parent
        checkout = main if bare else Path(git(cwd, 'rev-parse', '--show-toplevel')).resolve()
        origin = git(cwd, 'remote', 'get-url', 'origin')
        if origin:
            identity = 'origin:'+normalized_origin(origin)
        else:
            roots = sorted(git(cwd, 'rev-list', '--max-parents=0', 'HEAD').splitlines())
            if not roots:
                return None  # no stable root identity until the first commit
            identity = 'roots:'+','.join(roots)
        name = main.name.removesuffix('.git')[:120]
        public = {'project_id': hashlib.sha256(identity.encode()).hexdigest(), 'project': name,
                  'branch': git(cwd, 'branch', '--show-current')[:200],
                  'worktree': checkout.name[:120] if checkout != main else ''}
        return {'public': public, 'path': str(checkout), 'main_path': str(main), 'bare': bare}
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def registry_path():
    from .report import spool_path
    return spool_path().with_name('host-registry.sqlite3')


def connect():
    path = registry_path()
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    db = sqlite3.connect(path, timeout=.5)
    db.row_factory = sqlite3.Row
    db.executescript('''
      CREATE TABLE IF NOT EXISTS checkouts (
        project_id TEXT, path TEXT, main_path TEXT, name TEXT, branch TEXT,
        worktree TEXT, bare INTEGER, seen_at REAL, PRIMARY KEY(project_id,path));
      CREATE TABLE IF NOT EXISTS native_sessions (
        source TEXT, session TEXT, agent TEXT, native_id TEXT, cwd TEXT,
        project_id TEXT, seen_at REAL, PRIMARY KEY(source,session));
    ''')
    return db


def remember(cwd, agent=None, native_id=None):
    project = inspect(cwd)
    public = project['public'] if project else {'project': '', 'project_id': '', 'branch': '', 'worktree': ''}
    with closing(connect()) as db, db:
        if project:
            db.execute('INSERT OR REPLACE INTO checkouts VALUES(?,?,?,?,?,?,?,?)',
                (public['project_id'],project['path'],project['main_path'],public['project'],
                 public['branch'],public['worktree'],project['bare'],time.time()))
        if agent and native_id:
            from .report import identity
            source, session, _ = identity(agent, str(native_id))
            db.execute('INSERT OR REPLACE INTO native_sessions VALUES(?,?,?,?,?,?,?)',
                (source,session,agent,str(native_id),str(Path(cwd).resolve()) if cwd else '',
                 public['project_id'],time.time()))
    return public


def inventory():
    """Public inventory: one entry per known checkout, never its path or remote."""
    with closing(connect()) as db:
        return [{'project_id':r['project_id'],'name':r['name'],'branch':r['branch'],
                 'worktree':r['worktree']} for r in db.execute(
                     'SELECT * FROM checkouts ORDER BY project_id,worktree')]
