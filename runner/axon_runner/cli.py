"""Pull dashboard start jobs for this host and do only what host policy allows (#19).

The host pulls, so nothing on it listens for connections. This increment ships
the queue loop, host-local allowlist, and job-state reporting; the Claude start
path is wired to the verified `claude --bg -w <name> -n <name> "<prompt>"`
(checked against CLI 2.1.312), and agents without an executor yet are refused
with `no_executor` so no request is ever dropped silently."""
import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import config

START_TIMEOUT = 90


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


def start_claude(job, path, permission_mode, executable='claude'):
    """Background start inside the project; Claude creates the worktree itself
    with -w, and the session reports itself through its own hooks."""
    name = 'axon-' + job['id'][:8]
    command = [executable, '--bg', '-n', name, '--permission-mode', permission_mode]
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


STARTERS = {'claude': start_claude}


def decide(job, policy):
    """Apply host policy and, when allowed, run the agent's native start path."""
    path, mode, refusal = policy.resolve(job['agent'], job['project'])
    if refusal:
        return None, refusal
    starter = STARTERS.get(job['agent'])
    if starter is None:
        return None, 'no_executor'
    try:
        return (starter(job, path, mode, policy.executable.get(job['agent'])) or 'started'), None
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
        return 0 if isinstance(run_once(server, args.machine, credential, policy, 0), dict) else 1
    while True:
        if run_once(server, args.machine, credential, policy, 55) == 'unavailable':
            time.sleep(15)


if __name__ == '__main__':
    sys.exit(main())
