"""Pull dashboard start jobs for this host and do only what host policy allows (#19).

The host pulls, so nothing on it listens for connections. This increment ships
the queue loop, host-local allowlist, and job-state reporting. Claude starts via
the verified `claude --bg -n <name> -w <name> "<prompt>"` (CLI 2.1.312); Codex
via `codex exec --json -s <sandbox> -C <path>` (CLI 0.162.1), reporting started
once the JSONL stream yields `thread.started`. Pi starts via `pi -p
--session-id <id>` (CLI 1.4.0) inside a runner-managed git worktree when the job
asks for one. Agents without an executor
are refused with `no_executor` so no request is ever dropped silently."""
import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import config, resume

START_TIMEOUT = 90
# How long a fire-and-forget launcher watches for an immediate crash before it
# calls the launch good (Pi turns can legitimately finish inside this window).
LAUNCH_GRACE = 5


class StartError(Exception):
    pass


def read_credential():
    path = Path(os.environ.get('AXON_HOST_CREDENTIAL_FILE') or
                Path.home() / '.config' / 'axon' / 'host-credential')
    try:
        return path.read_text().strip()
    except OSError:
        return None


def post(url, payload=None, credential=None, timeout=15):
    data = json.dumps(payload).encode() if payload is not None else b''
    headers = {'Content-Type': 'application/json'} if payload is not None else {}
    if credential:
        headers['Authorization'] = 'Bearer ' + credential
    request = urllib.request.Request(url, data=data, method='POST', headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw) if raw else {}


def start_claude(job, path, settings, executable=None):
    """Background start inside the project; Claude creates the worktree itself
    with -w, and the session reports itself through its own hooks."""
    if settings['permission_mode'] not in config.PERMISSION_MODES:
        raise StartError('bad_permission_mode')
    name = 'axon-' + job['id'][:8]
    command = [executable or 'claude', '--bg', '-n', name,
               '--permission-mode', settings['permission_mode']]
    if job['worktree']:
        command += ['-w', name]
    command.append(job['prompt'])
    try:
        completed = subprocess.run(command, cwd=path, capture_output=True, text=True, timeout=START_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise StartError('launch_timeout') from None
    if completed.returncode:
        tail = (completed.stderr or completed.stdout or '').strip().replace('\n', ' ')[-180:]
        raise StartError('launch_failed: ' + (tail or 'exit %d' % completed.returncode))
    return 'launched as %s' % name


def log_dir():
    """Host-local per-job log directory (0700; never a pipe an orphan could block on)."""
    dir_path = Path(os.environ.get('AXON_ACTIVITY_HOME', Path.home() / '.local' / 'state' / 'axon')) / 'runner'
    dir_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return dir_path


def thread_id_from(log_text):
    """First `thread.started` thread id in a codex --json stream; None if absent."""
    for line in log_text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get('type') == 'thread.started' and event.get('thread_id'):
            return str(event['thread_id'])[:64]
    return None


def start_codex(job, path, settings, executable=None):
    """Headless session through the agent's own exec; it stays on this host and
    reports itself through its hooks. Output goes to a host-local per-job log
    (never a pipe) so an orphaned child can never block on a closed reader.
    Sandbox is capped by config.SANDBOXES; bypass flags are never passed."""
    sandbox = settings.get('sandbox', 'workspace-write')
    if sandbox not in config.SANDBOXES:
        raise StartError('bad_sandbox')
    command = [executable or 'codex', 'exec', '--json', '-s', sandbox, '-C', str(path)]
    if job['worktree']:
        command.append('--worktree')
    command.append(job['prompt'])
    log_path = log_dir() / ('job-%s.log' % job['id'][:8])
    try:
        with log_path.open('a', encoding='utf-8', errors='replace') as log_file:
            proc = subprocess.Popen(command, cwd=str(path), stdin=subprocess.DEVNULL,
                                    stdout=log_file, stderr=subprocess.STDOUT)
    except OSError as error:
        raise StartError('launch_failed: %s' % type(error).__name__) from None
    deadline = time.time() + START_TIMEOUT
    text = ''
    while time.time() < deadline:
        try:
            text = log_path.read_text(errors='replace')
        except OSError:
            pass
        thread = thread_id_from(text)
        if thread:
            return 'thread ' + thread
        if proc.poll() is not None:
            tail = text.strip().replace('\n', ' ')[-180:]
            raise StartError('launch_failed: ' + (tail or 'exit %d' % proc.returncode))
        time.sleep(0.05)
    proc.kill()
    raise StartError('launch_timeout')


def prepare_worktree(job, path, settings):
    """Runner-managed git worktree for agents with no native flag (Pi). Lives
    under settings['worktree_root'] (default: the host's axon state dir), on a
    per-job branch, so the main checkout is never the working tree."""
    root = Path(settings.get('worktree_root')
                or Path(os.environ.get('AXON_ACTIVITY_HOME', Path.home() / '.local' / 'state' / 'axon'))
                / 'worktrees')
    wt_path = root / ('%s-%s' % (Path(path).name, job['id'][:8]))
    if wt_path.is_dir():
        return wt_path
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        completed = subprocess.run(['git', 'worktree', 'add', str(wt_path), '-b',
                                    'axon-' + job['id'][:8]],
                                   cwd=str(path), capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        raise StartError('worktree_setup_failed') from None
    if completed.returncode:
        tail = (completed.stderr or completed.stdout or '').strip().replace('\n', ' ')[-180:]
        raise StartError('worktree_setup_failed: ' + (tail or 'exit %d' % completed.returncode))
    return wt_path


def start_pi(job, path, settings, executable=None):
    """Non-interactive Pi run (CLI 1.4.0: `-p` prints and exits, `--session-id`
    creates the session when missing). The session id is the runner's job, so
    the Pi plugin's hooks report it on the board under a stable key. Pi has no
    worktree flag, so a worktree job runs inside a runner-managed git worktree.
    Output goes to the host-local per-job log; a fast nonzero exit before the
    grace window is a launch failure, a fast zero exit is a completed turn."""
    session = 'axon-' + job['id'][:8]
    if job['worktree']:
        path = prepare_worktree(job, path, settings)
    command = [executable or 'pi', '-p', '--session-id', session, job['prompt']]
    log_path = log_dir() / ('job-%s.log' % job['id'][:8])
    try:
        with log_path.open('a', encoding='utf-8', errors='replace') as log_file:
            proc = subprocess.Popen(command, cwd=str(path), stdin=subprocess.DEVNULL,
                                    stdout=log_file, stderr=subprocess.STDOUT)
    except OSError as error:
        raise StartError('launch_failed: %s' % type(error).__name__) from None
    deadline = time.time() + LAUNCH_GRACE
    while time.time() < deadline:
        if proc.poll() is not None:
            if proc.returncode:
                tail = log_path.read_text(errors='replace').strip().replace('\n', ' ')[-180:]
                raise StartError('launch_failed: ' + (tail or 'exit %d' % proc.returncode))
            break
        time.sleep(0.05)
    return 'session ' + session


STARTERS = {'claude': start_claude, 'codex': start_codex, 'pi': start_pi}


def decide(job, policy):
    """Apply host policy and, when allowed, run the agent's native start path."""
    path, settings, refusal = policy.resolve(job['agent'], job['project'])
    if refusal:
        return None, refusal
    starter = STARTERS.get(job['agent'])
    if starter is None:
        return None, 'no_executor'
    try:
        return (starter(job, path, settings, policy.executable.get(job['agent'])) or 'started'), None
    except StartError as error:
        return None, str(error)


def run_once(server, machine, credential, policy, wait, log=print):
    """Claim one job and report its outcome; returns the job or None."""
    base = server.rstrip('/')
    try:
        reply = post('%s/hosts/%s/jobs/claim?wait=%d' % (base, urllib.parse.quote(machine), wait),
                     credential=credential, timeout=wait + 15)
    except OSError as error:
        log('server unavailable: %s' % type(error).__name__)
        return 'unavailable'
    job = reply.get('job')
    if not job:
        return None
    detail, refusal = decide(job, policy)
    status = 'refused' if refusal else 'started'
    try:
        post('%s/hosts/%s/jobs/%s/report' % (base, urllib.parse.quote(machine), job['id']),
             payload={'status': status, 'detail': (refusal or detail)[:512]}, credential=credential)
    except OSError as error:
        log('could not report job %s: %s' % (job['id'][:8], type(error).__name__))
    log('job %s (%s/%s) %s%s' % (job['id'][:8], job['agent'], job['project'], status,
                                 ': ' + refusal if refusal else ''))
    return job


def resume_loop(server, machine, credential, policy):
    """Long-poll for input to sessions that ended, beside the start-job loop."""
    while True:
        try:
            outcome = resume.run_once(server, machine, credential, policy, 55, post, log_dir=log_dir())
        except Exception as error:  # one bad claim must not end resuming for the host
            print('resume error: %s' % type(error).__name__, file=sys.stderr)
            outcome = 'unavailable'
        if outcome == 'unavailable':
            time.sleep(15)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='axon-runner', description=__doc__)
    parser.add_argument('--server', help='Journal server URL (default: config file, '
                        'then AXON_SERVER, then http://$AXON_BROKER:8510)')
    parser.add_argument('--machine', default=os.environ.get('AXON_MACHINE') or socket.gethostname())
    parser.add_argument('--config', help='runner.toml path (default ~/.config/axon/runner.toml)')
    parser.add_argument('--once', action='store_true', help='claim at most one job and exit')
    args = parser.parse_args(argv)
    policy = config.load(args.config)
    server = (args.server or policy.server or os.environ.get('AXON_SERVER')
              or 'http://%s:8510' % os.environ.get('AXON_BROKER', 'localhost'))
    credential = read_credential()
    if not credential:
        print('No host credential; enroll this host first (see plugins README).', file=sys.stderr)
        return 1
    if args.once:
        claimed = run_once(server, args.machine, credential, policy, 0)
        if policy.resume and policy.resume['agents']:
            claimed = resume.run_once(server, args.machine, credential, policy, 0, post, log_dir=log_dir()) or claimed
        return 0 if isinstance(claimed, dict) else 1
    if policy.resume and policy.resume['agents']:
        threading.Thread(target=resume_loop, args=(server, args.machine, credential, policy),
                         daemon=True, name='resume').start()
    while True:
        if run_once(server, args.machine, credential, policy, 55) == 'unavailable':
            time.sleep(15)


if __name__ == '__main__':
    sys.exit(main())
