#!/usr/bin/env python3
"""Keep a server host on a branch: build each new server commit into its own release, then switch and verify.

Releases live in ROOT/releases/<version>-<sha7>/venv and ROOT/current points at the live one. A commit is
deployed only once CI passed on it, the database is backed up first, and a failed health check switches back.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
from urllib.request import Request, urlopen

# Only these paths reach the installed server; other commits are recorded without a restart.
SERVER_PATHS = ('server/axon_server', 'server/pyproject.toml', 'packages/python/axon_protocol')
PASSED = {'success', 'neutral', 'skipped'}
# The server's POST /update writes this file. A requested run keeps waiting for CI instead of leaving it to the
# next timer tick; the interval and deadline cap it at a few dozen GitHub requests however often it is kicked.
REQUEST = 'update-requested'
REQUEST_WAIT = 20*60
REQUEST_INTERVAL = 45


def log(message):
    print(message, flush=True)


def git(mirror, *args):
    return subprocess.run(['git', '--git-dir', str(mirror), *args], check=True, capture_output=True, text=True).stdout.strip()


def fetch(mirror, repo, branch):
    if not mirror.exists():
        url = repo if repo.startswith('/') else f'https://github.com/{repo}.git'
        subprocess.run(['git', 'clone', '--bare', '--quiet', url, str(mirror)], check=True)
    git(mirror, 'fetch', '--quiet', '--prune', 'origin', f'+refs/heads/{branch}:refs/heads/{branch}')
    return git(mirror, 'rev-parse', branch)


def ci_state(repo, sha, opener=urlopen):
    """'passed', 'failed' or 'pending' for every check run GitHub has for this commit."""
    request = Request(f'https://api.github.com/repos/{repo}/commits/{sha}/check-runs?per_page=100',
                      headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'axon-update'})
    with opener(request, timeout=20) as response:
        runs = json.load(response)['check_runs']
    if not runs or any(run['status'] != 'completed' for run in runs):
        return 'pending'
    return 'passed' if all(run['conclusion'] in PASSED for run in runs) else 'failed'


def touches_server(mirror, old, new):
    if not old:
        return True
    try:
        return bool(git(mirror, 'diff', '--name-only', old, new, '--', *SERVER_PATHS))
    except subprocess.CalledProcessError:
        return True  # The old commit is unknown (rewritten history): rebuild rather than guess.


def load_state(root):
    try:
        return json.loads((root/'update-state.json').read_text())
    except (OSError, ValueError):
        return {}


def save_state(root, state):
    temporary = root/'update-state.json.tmp'
    temporary.write_text(json.dumps(state, indent=2)+'\n')
    os.replace(temporary, root/'update-state.json')


def deployed_commit(root, mirror, state):
    # The live release name carries its short commit, and stays true when someone deploys by hand.
    try:
        short = (root/'current').resolve().name.rsplit('-', 1)[1]
        if state.get('commit', '').startswith(short):
            return state['commit']
        return git(mirror, 'rev-parse', '--verify', short+'^{commit}')
    except (IndexError, subprocess.CalledProcessError):
        return state.get('commit')


def build(mirror, sha, releases, python):
    with tempfile.TemporaryDirectory(prefix='axon-update-') as tmp:
        source = Path(tmp)
        archive = source/'source.tar'
        git(mirror, 'archive', '--output', str(archive), sha, 'server', 'packages/python/axon_protocol')
        with tarfile.open(archive) as tar:
            tar.extractall(source, filter='data')
        version = tomllib.loads((source/'server/pyproject.toml').read_text())['project']['version']
        release = releases/f'{version}-{sha[:7]}'
        if release.exists():
            shutil.rmtree(release)
        release.mkdir(parents=True)
        try:
            subprocess.run([python, '-m', 'venv', str(release/'venv')], check=True)
            subprocess.run([str(release/'venv/bin/pip'), 'install', '--quiet', '--disable-pip-version-check',
                            str(source/'packages/python/axon_protocol'), str(source/'server')], check=True)
        except BaseException:
            shutil.rmtree(release, ignore_errors=True)
            raise
        return release


def backup(db, backups, sha):
    if not db.exists():
        return None
    backups.mkdir(parents=True, exist_ok=True)
    target = backups/f'activity-before-{sha[:7]}-{time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())}.sqlite3'
    with sqlite3.connect(f'file:{db}?mode=ro', uri=True) as source, sqlite3.connect(target) as copy:
        source.backup(copy)
    return target


def point(root, release):
    link = root/'current.tmp'
    link.unlink(missing_ok=True)
    link.symlink_to(Path('releases')/release.name)
    os.replace(link, root/'current')


def healthy(url, timeout, opener=urlopen):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        try:
            with opener(url, timeout=5) as response:
                if response.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(1)
    return False


def restart(unit):
    subprocess.run(['systemctl', '--user', 'restart', unit], check=True)


def prune(root, keep, backups, db):
    """Keep the newest releases and backups; the live and previous releases always stay."""
    live = (root/'current').resolve().name
    releases = sorted((path for path in (root/'releases').iterdir() if path.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
    for release in releases[keep:]:
        if release.name != live:
            shutil.rmtree(release, ignore_errors=True)
    for stage in root.glob('staging-*'):
        shutil.rmtree(stage, ignore_errors=True)
    copies = [*backups.glob('activity-before-*.sqlite3'), *db.parent.glob('activity-before-*.sqlite3')]
    for old in sorted(copies, key=lambda p: p.stat().st_mtime, reverse=True)[keep:]:
        old.unlink(missing_ok=True)


def update(args, ci=ci_state, check=healthy, restart_unit=restart):
    root = args.root
    mirror = root/'source.git'
    state = load_state(root)
    head = fetch(mirror, args.repo, args.branch)
    current = deployed_commit(root, mirror, state)
    if head == current:
        if state.get('commit') != head:  # deployed by hand: record it rather than rebuild the live virtualenv
            state.update(commit=head, seen=head, release=(root/'current').resolve().name)
            save_state(root, state)
        return 'current'
    if head in (state.get('seen'), state.get('failed')):
        return 'current'
    if not touches_server(mirror, current, head):
        state.update(commit=head, seen=head)
        save_state(root, state)
        log(f'{head[:7]}: no server changes, nothing to restart')
        return 'unchanged'
    if args.require_ci:
        result = ci(args.repo, head)
        if result == 'pending':
            log(f'{head[:7]}: waiting for CI')
            return 'pending'
        if result == 'failed':
            state['failed'] = head
            save_state(root, state)
            log(f'{head[:7]}: CI failed, not deploying')
            return 'failed'
    previous = (root/'current').resolve() if (root/'current').exists() else None
    release = build(mirror, head, root/'releases', args.python)
    copy = backup(args.db, args.backups, head)
    point(root, release)
    restart_unit(args.unit)
    if not check(args.health_url, args.health_timeout):
        log(f'{head[:7]}: health check failed, switching back to {previous.name if previous else "nothing"}')
        if previous:
            point(root, previous)
            restart_unit(args.unit)
        shutil.rmtree(release, ignore_errors=True)
        state['failed'] = head
        save_state(root, state)
        return 'rolled back'
    state.update(commit=head, seen=head, release=release.name, backup=str(copy) if copy else None,
                 previous=previous.name if previous else None, deployed_at=time.time())
    state.pop('failed', None)
    save_state(root, state)
    prune(root, args.keep, args.backups, args.db)
    # The updater ships with the server, so each deploy also installs its own newest version.
    if args.self_path:
        newer = git(mirror, 'show', f'{head}:server/deploy/axon_update.py')+'\n'
        if args.self_path.read_text() != newer:
            args.self_path.write_text(newer)
    log(f'{head[:7]}: deployed {release.name}')
    return 'deployed'


def run(args, step=update, sleep=time.sleep, clock=time.time):
    request = args.root/REQUEST
    def requested_at():
        try:
            return request.stat().st_mtime
        except OSError:
            return None
    while True:
        requested = requested_at()
        if requested is None or clock()-requested > REQUEST_WAIT:
            request.unlink(missing_ok=True)
            return step(args)
        while True:
            try:
                result = step(args)
            except OSError as error:  # GitHub briefly unreachable or rate limited: keep waiting, within the deadline.
                log(f'check failed, retrying: {error}')
                result = 'pending'
            if result != 'pending' or clock()-requested+REQUEST_INTERVAL > REQUEST_WAIT:
                break
            sleep(REQUEST_INTERVAL)
        # A request made during this run may name a newer commit than the one just handled.
        if requested_at() == requested:
            request.unlink(missing_ok=True)
            return result


def parser():
    home = Path.home()
    env = os.environ.get
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--repo', default=env('AXON_UPDATE_REPO', 'ehippy/axon'))
    result.add_argument('--branch', default=env('AXON_UPDATE_BRANCH', 'main'))
    result.add_argument('--root', type=Path, default=Path(env('AXON_UPDATE_ROOT', home/'.local/share/axon/server')))
    result.add_argument('--db', type=Path, default=Path(env('AXON_UPDATE_DB', home/'.local/state/axon/activity.sqlite3')))
    result.add_argument('--backups', type=Path, default=Path(env('AXON_UPDATE_BACKUPS', home/'.local/state/axon/backups')))
    result.add_argument('--unit', default=env('AXON_UPDATE_UNIT', 'axon-server'))
    result.add_argument('--health-url', default=env('AXON_UPDATE_HEALTH_URL', 'http://127.0.0.1:8510/health'))
    result.add_argument('--health-timeout', type=float, default=30)
    result.add_argument('--python', default=env('AXON_UPDATE_PYTHON', '/usr/bin/python3'))
    result.add_argument('--keep', type=int, default=int(env('AXON_UPDATE_KEEP', '5')))
    result.add_argument('--no-ci', dest='require_ci', action='store_false', help='Deploy without waiting for CI to pass')
    result.add_argument('--no-self-update', dest='self_path', action='store_const', const=None, default=Path(__file__).resolve())
    return result


if __name__ == '__main__':
    try:
        run(parser().parse_args())
    except subprocess.CalledProcessError as error:
        log(f'update failed: {error}')
        sys.exit(1)
