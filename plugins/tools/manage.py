#!/usr/bin/env python3
"""Build self-contained Axon plugins or install them without replacing unrelated hooks."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from xml.sax.saxutils import escape as xml_escape

ROOT = Path(__file__).resolve().parents[2]
for project in ('packages/python/axon_protocol', 'packages/python/axon_reporter'):
    path = str(ROOT/project)
    if path not in sys.path:
        sys.path.insert(0, path)
from axon_reporter.report import MACHINE_ALIAS_AUTO, detected_machine_alias, setting
from axon_reporter.credentials import enroll

AGENTS = ('codex','claude','hermes','pi','opencode')


def build(agent, destination):
    if agent not in AGENTS:
        raise ValueError('Unknown agent')
    source = ROOT/'plugins'/agent
    destination = Path(destination).resolve()
    # Only replace recognized generated packages, never arbitrary user directories.
    if destination.exists() and any(destination.iterdir()) and not (
            (destination/'runtime').is_dir() and any((destination/name).is_file()
            for name in ('plugin.json', '.claude-plugin/plugin.json', 'plugin.yaml', 'package.json', 'plugin/axon.js'))):
        raise ValueError('Build destination must be empty or an existing Axon package')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.axon-build-', dir=destination.parent) as tmp:
        staged = Path(tmp)/'package'
        staged.mkdir()
        selected = ['README.md','__init__.py','plugin.yaml'] if agent == 'hermes' else \
            ['README.md','index.ts','package.json','hooks','skills'] if agent == 'pi' else \
            ['README.md','plugin','hooks','skills'] if agent == 'opencode' else \
            ['README.md','hooks','skills', 'plugin.json' if agent=='codex' else '.claude-plugin']
        if agent == 'claude':
            selected.append('usage')
        for name in selected:
            src, dst = source/name, staged/name
            if src.is_dir():
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns('__pycache__'))
            else:
                shutil.copy2(src, dst)
        for name in ('axon_protocol', 'axon_reporter'):
            shutil.copytree(ROOT/'packages/python'/name/name, staged/'runtime'/name,
                            ignore=shutil.ignore_patterns('__pycache__'))
        collector = staged/'usage/bin/collect'
        if collector.exists():
            # Claude runs the collector without a shell; pin the building interpreter.
            lines = collector.read_text().split('\n', 1)
            collector.write_text('#!'+sys.executable+'\n'+lines[1])
        if destination.exists():
            destination.rename(Path(tmp)/'previous')
        staged.rename(destination)
    return destination


def ours(command):
    try:
        words=shlex.split(command)
    except (ValueError,TypeError):
        return False
    for word in words:
        path=word.replace('\\','/')
        if '/.local/share/axon/plugins/' in path and path.endswith('/hooks/run.py'):
            return True
    return False


def merged_config(current,agent,package):
    if not isinstance(current,dict) or not isinstance(current.get('hooks',{}),dict):
        raise ValueError('Existing settings must contain a hooks object')
    result=json.loads(json.dumps(current))
    hooks=result.setdefault('hooks',{})
    for event, groups in hooks.items():
        if not isinstance(groups,list):
            raise ValueError(f'Invalid existing hook groups for {event}')
        kept=[]
        for group in groups:
            if not isinstance(group,dict) or not isinstance(group.get('hooks',[]),list):
                raise ValueError(f'Invalid existing hook group for {event}')
            handlers=[h for h in group.get('hooks',[]) if not isinstance(h,dict) or not ours(h.get('command',''))]
            if handlers or not group.get('hooks'):
                kept.append({**group,'hooks':handlers})
        hooks[event]=kept
    bundled=json.loads((ROOT/'plugins'/agent/'hooks/hooks.json').read_text())['hooks']
    for event, groups in bundled.items():
        for group in groups:
            for handler in group['hooks']:
                # Keep bundled arguments (e.g. `listen`) after the runner path.
                words=shlex.split(handler['command'])
                extra=next((words[i+1:] for i,w in enumerate(words) if w.endswith('/hooks/run.py')),[])
                handler['command']=shlex.join([sys.executable,str(package/'hooks/run.py'),*extra])
        hooks.setdefault(event,[]).extend(groups)
    if agent=='claude':
        # Usage reaches only hooks modules, which load from plugin folders, not settings hooks.
        env=result.setdefault('env',{})
        if not isinstance(env,dict) or not isinstance(env.get('CLAUDE_CODE_PLUGIN_DIRS',''),str):
            raise ValueError('Existing settings env must map names to strings')
        dirs=[d for d in env.get('CLAUDE_CODE_PLUGIN_DIRS','').split(os.pathsep)
              if d and not d.replace('\\','/').rstrip('/').endswith('/.local/share/axon/plugins/claude/usage')]
        env['CLAUDE_CODE_PLUGIN_DIRS']=os.pathsep.join([*dirs,str(package/'usage')])
    return result


def write_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.axon-tmp')
    tmp.write_text(json.dumps(value,indent=2)+'\n')
    tmp.chmod(path.stat().st_mode & 0o777 if path.exists() else 0o600)
    tmp.replace(path)


MACHINE_LABEL = 'AXON_MACHINE'


def machine_label_path(home):
    """Reporter settings file the reporter itself reads, honouring its override."""
    override = os.environ.get('AXON_CONFIG_FILE')
    return Path(override).expanduser() if override else Path(home).expanduser()/'.config/axon/env'


def read_machine_label(path):
    """Current AXON_MACHINE value, or None when the setting is absent."""
    try:
        lines = path.read_text().splitlines()
    except (OSError, ValueError):
        return None
    for line in lines:
        words = shlex.split(line, comments=True)
        if words[:1] == ['export']:
            words = words[1:]
        if len(words) == 1 and words[0].startswith(MACHINE_LABEL+'='):
            return words[0].split('=', 1)[1]
    return None


def ensure_machine_label(home, apply=False):
    """Give this machine a readable host label without overriding an explicit choice.

    Unset means the reporter keeps its private random label, so an install is the
    deliberate opt-in to host names; `auto` resolves the host name per report and
    tracks renames, while a literal value pins one. Existing settings are never
    rewritten, and a label change only ever affects events reported afterwards.
    """
    path = machine_label_path(home)
    current = read_machine_label(path)
    if current is not None:
        return {'value': current, 'path': str(path), 'written': False, 'hostname': hostname_preview()}
    if apply:
        created = not path.parent.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        if created:
            path.parent.chmod(0o700)
        previous = path.read_text() if path.exists() else ''
        tmp = path.with_name(path.name+'.axon-tmp')
        tmp.write_text((previous.rstrip('\n')+'\n' if previous.strip() else '')+MACHINE_LABEL+'=auto\n')
        tmp.chmod(path.stat().st_mode & 0o777 if path.exists() else 0o600)
        tmp.replace(path)
    return {'value': MACHINE_ALIAS_AUTO, 'path': str(path), 'written': bool(apply),
            'hostname': hostname_preview()}


def hostname_preview():
    """Resolved host name for install output, or None when it cannot be determined."""
    return detected_machine_alias()


def install(agent, home, apply=False, enable=True, hermes_command='hermes'):
    home=Path(home).expanduser().resolve()
    package=home/'.local/share/axon/plugins'/agent
    settings=home/'.pi/agent/settings.json' if agent=='pi' else \
        home/'.config/opencode/opencode.json' if agent=='opencode' else \
        home/f'.{agent}'/('hooks.json' if agent=='codex' else 'settings.json' if agent=='claude' else 'config.yaml')
    # Read malformed configuration before making any changes.
    current=json.loads(settings.read_text()) if agent not in ('hermes','pi','opencode') and settings.exists() else {}
    if agent not in ('hermes','pi','opencode') and (not isinstance(current,dict) or not isinstance(current.get('hooks',{}),dict)):
        raise ValueError('Invalid existing settings; nothing changed')
    proposed=merged_config(current,agent,package) if agent not in ('hermes','pi','opencode') else None
    plan={'agent':agent,'package':str(package),'settings':str(settings),'apply':apply,
          'machine_label':ensure_machine_label(home)}
    if not apply:
        return plan
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    backup=home/'.local/state/axon/install-backups'/f'{stamp}-{agent}'
    backup.mkdir(parents=True,mode=0o700)
    if settings.exists():
        shutil.copy2(settings,backup/settings.name)
    if package.exists():
        shutil.copytree(package,backup/'package',symlinks=True)
    build(agent,package)
    if agent=='hermes':
        target=home/'.hermes/plugins/axon-activity'
        target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists() or target.is_symlink():
            target.rename(backup/'hermes-plugin')
        target.symlink_to(package,target_is_directory=True)
        if enable:
            # Hermes owns its configuration format and preserves other enabled plugins.
            search_path = os.environ.get('PATH', '') + os.pathsep + os.pathsep.join(str(home/d) for d in EXTRA_BIN_DIRS)
            executable = shutil.which(hermes_command, path=search_path) or hermes_command
            subprocess.run([executable,'plugins','enable','axon-activity'],check=True,stdout=sys.stderr)
    elif agent=='pi':
        # Pi auto-discovers extension directories; the package.json manifest
        # points at index.ts. No configuration file is rewritten.
        target=home/'.pi/agent/extensions/axon-activity'
        target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists() or target.is_symlink():
            target.rename(backup/'pi-extension')
        target.symlink_to(package,target_is_directory=True)
        skill=home/'.pi/agent/skills/axon-activity'
        skill.parent.mkdir(parents=True,exist_ok=True)
        if skill.exists() or skill.is_symlink():
            skill.rename(backup/'skill')
        skill.symlink_to(package/'skills/axon-activity',target_is_directory=True)
    elif agent=='opencode':
        # opencode auto-loads JS/TS modules from the global plugins directory;
        # skills live under skills/<name>/SKILL.md. No config file is rewritten.
        target=home/'.config/opencode/plugins/axon-activity.js'
        target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists() or target.is_symlink():
            target.rename(backup/'opencode-plugin')
        # Symlink, not copy: the plugin resolves its own realpath to find
        # hooks/run.py inside this package.
        target.symlink_to(package/'plugin/axon.js')
        skill=home/'.config/opencode/skills/axon-activity'
        skill.parent.mkdir(parents=True,exist_ok=True)
        if skill.exists() or skill.is_symlink():
            skill.rename(backup/'skill')
        skill.symlink_to(package/'skills/axon-activity',target_is_directory=True)
    else:
        write_json(settings,proposed)
        skill=home/f'.{agent}/skills/axon-activity'
        skill.parent.mkdir(parents=True,exist_ok=True)
        if skill.exists() or skill.is_symlink():
            skill.rename(backup/'skill')
        skill.symlink_to(package/'skills/axon-activity',target_is_directory=True)
    return {**plan,'machine_label':ensure_machine_label(home,apply),'backup':str(backup),
            'restart_required':True,'hook_review_required':agent=='codex'}


# Install locations agents use that user shells add late (or cron never adds).
EXTRA_BIN_DIRS = ('.local/bin', '.npm-global/bin', '.opencode/bin')


def detect_agents(home=Path.home()):
    """Agents whose binary exists on PATH or in a well-known user bin dir."""
    home = Path(home).expanduser()
    path = os.environ.get('PATH', '') + os.pathsep + os.pathsep.join(str(home/d) for d in EXTRA_BIN_DIRS)
    return [agent for agent in AGENTS if shutil.which(agent, path=path)]


def sync(home=Path.home(), apply=False, force=False):
    """Install hooks for every detected agent; skip cleanly when nothing moved.

    A stamp under the activity state dir records the checkout revision plus
    the detected agent set, so machine-bootstrap loops (cron, chezmoi
    run_after) can invoke this on every run and pay only for real changes.
    """
    home = Path(home).expanduser().resolve()
    try:
        revision = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'],
                                  capture_output=True, text=True).stdout.strip() or 'unknown'
    except OSError:
        revision = 'unknown'
    agents = detect_agents(home)
    state = Path(os.environ.get('AXON_ACTIVITY_HOME', home/'.local/state/axon'))
    stamp = state/'hooks-synced'
    want = f'{revision} {" ".join(agents)}\n'
    label = ensure_machine_label(home, apply)
    # During the migration, older servers have no enrollment API. Retry on every sync,
    # even a stamp-skipped one, so credentials arrive as soon as the server does.
    if apply:
        url = setting('AXON_SERVER') or 'http://%s:8510' % setting('AXON_BROKER', 'localhost')
        try:
            with urllib.request.urlopen(url.rstrip('/')+'/health', timeout=1) as response:
                supports_enrollment = json.load(response).get('host_claims', {}).get('enrollment', False)
        except (OSError, ValueError):
            supports_enrollment = False
        if supports_enrollment:
            enroll(home)
    if not force and agents and stamp.exists() and stamp.read_text() == want:
        return {'revision': revision, 'agents': agents, 'machine_label': label, 'skipped': True}
    installed, failed = [], {}
    for agent in agents:
        try:
            install(agent, home, apply=apply)
            if apply:
                installed.append(agent)
        except Exception as error:  # noqa: BLE001 — one bad agent must not stop the rest
            failed[agent] = str(error)
    if apply and not failed:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(want)
    return {'revision': revision, 'agents': agents, 'apply': apply,
            'machine_label': ensure_machine_label(home, apply),
            'installed': installed, 'failed': failed, 'skipped': False}


def state_dir(home):
    return Path(os.environ.get('AXON_ACTIVITY_HOME', Path(home).expanduser()/'.local/state/axon'))


def timer_files(home, interval=15, system=None):
    """Unit or agent files for this platform, rendered from plugins/deploy."""
    home = Path(home).expanduser()
    system = system or platform.system()
    if interval < 1:
        raise ValueError('Interval must be at least one minute')
    values = {'python': sys.executable, 'manage': str(ROOT/'plugins/tools/manage.py'),
              'path': os.environ.get('PATH', '/usr/bin:/bin'), 'interval': interval,
              'seconds': interval*60, 'log': str(state_dir(home)/'sync.log')}
    deploy = ROOT/'plugins/deploy'
    if system == 'Darwin':
        escaped = {k: xml_escape(str(v)) for k, v in values.items()}
        return {home/'Library/LaunchAgents/org.axon.sync.plist':
                (deploy/'org.axon.sync.plist').read_text().format(**escaped)}
    if system == 'Linux':
        units = home/'.config/systemd/user'
        return {units/name: (deploy/name).read_text().format(**values)
                for name in ('axon-sync.service', 'axon-sync.timer')}
    raise ValueError(f'No user timer for {system}; run `manage.py update --apply` from cron')


def install_timer(home=Path.home(), apply=False, interval=15, system=None, run=subprocess.run):
    """Install a user timer that runs `update --apply`; without apply, only plan."""
    home = Path(home).expanduser()
    system = system or platform.system()
    files = timer_files(home, interval, system)
    if system == 'Darwin':
        plist = next(iter(files))
        domain = f'gui/{os.getuid()}'
        commands = [['launchctl', 'bootout', f'{domain}/org.axon.sync'],
                    ['launchctl', 'bootstrap', domain, str(plist)]]
    else:
        commands = [['systemctl', '--user', 'daemon-reload'],
                    ['systemctl', '--user', 'enable', '--now', 'axon-sync.timer']]
    plan = {'files': [str(p) for p in files], 'commands': [shlex.join(c) for c in commands],
            'interval_minutes': interval, 'apply': apply}
    if not apply:
        return plan
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    state_dir(home).mkdir(parents=True, exist_ok=True)
    for i, command in enumerate(commands):
        # bootout only clears a previous load, so its failure is expected on first install.
        run(command, check=not (system == 'Darwin' and i == 0), capture_output=True)
    return plan


def server_commit(timeout=10):
    """The journal server's deployed commit, or None when unknown."""
    url = setting('AXON_SERVER') or 'http://%s:8510' % setting('AXON_BROKER', 'localhost')
    try:
        with urllib.request.urlopen(url.rstrip('/')+'/update', timeout=timeout) as response:
            commit = json.load(response).get('commit')
    except (OSError, ValueError, AttributeError):
        return None
    return commit if isinstance(commit, str) and commit else None


def git(root, *args, check=True):
    return subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, check=check)


def run_sync(home, root):
    """Sync with the freshly pulled code, not the code already loaded here."""
    done = subprocess.run([sys.executable, str(Path(root)/'plugins/tools/manage.py'), 'sync', '--apply',
                           '--home', str(home)], capture_output=True, text=True)
    try:
        report = json.loads(done.stdout)
    except ValueError:
        report = {}
    if done.returncode:
        raise RuntimeError(f'sync failed: {report.get("failed") or done.stderr.strip()}')
    return report


def publish_sync_status(result):
    """Publish bounded public facts; keep local paths and command errors in the local log."""
    from axon_reporter.credentials import read_credential
    credential = read_credential()
    if not credential:
        return
    payload = {'synced_revision': result.get('synced_revision', result['revision']), 'ok': result['ok'],
               'error': None if result['ok'] else result.get('reason', 'sync_failed')}
    url = setting('AXON_SERVER') or 'http://%s:8510' % setting('AXON_BROKER', 'localhost')
    request = urllib.request.Request(url.rstrip('/')+'/hosts/sync',data=json.dumps(payload).encode(),
        headers={'Authorization': 'Bearer '+credential, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=3) as response:
        response.read()


def update(home=Path.home(), apply=False, root=ROOT, server=server_commit, sync_fn=run_sync):
    """Fast-forward the managed clone, hold it at the server's commit, then sync.

    Hooks that add protocol fields need the server deployed first, so a host
    never runs a revision the server's `/update` commit does not contain.
    """
    home = Path(home).expanduser().resolve()
    deployed = server()
    revision = git(root, 'rev-parse', 'HEAD', check=False).stdout.strip() or 'unknown'
    if not apply:
        return {'root': str(root), 'revision': revision, 'server_commit': deployed, 'apply': False}
    held, error, report, reason = False, None, {}, None
    try:
        if git(root, 'status', '--porcelain', '--untracked-files=no').stdout.strip():
            raise RuntimeError(f'{root} has local changes; keep a dedicated clone for the timer')
        if deployed is None:
            reason = 'server_unavailable'
            raise RuntimeError('server commit unknown; keeping the current hooks')
        pulled = git(root, 'pull', '-q', '--ff-only', check=False)
        if pulled.returncode:
            raise RuntimeError('git pull failed: '+pulled.stderr.strip())
        if git(root, 'merge-base', '--is-ancestor', 'HEAD', deployed, check=False).returncode:
            if git(root, 'cat-file', '-e', deployed+'^{commit}', check=False).returncode:
                # Cannot prove the pull is not ahead of the server: undo it.
                git(root, 'reset', '-q', '--hard', revision)
                raise RuntimeError(f'server commit {deployed} is not in this clone')
            git(root, 'reset', '-q', '--hard', deployed)
            held = True
        revision = git(root, 'rev-parse', 'HEAD').stdout.strip()
        report = sync_fn(home, root)
    except (RuntimeError, OSError, subprocess.CalledProcessError) as failure:
        error = str(failure)
    status = {'revision': revision, 'at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'ok': error is None, 'error': error}
    stamp = state_dir(home)/'hooks-synced'
    try:
        synced_revision = stamp.read_text().split()[0]
    except (OSError, IndexError):
        synced_revision = revision if error is None else 'unknown'
    status['synced_revision'] = synced_revision
    write_json(state_dir(home)/'sync-status.json', status)
    try:
        publish_sync_status({**status, 'reason': reason or 'sync_failed'})
    except (OSError, ValueError):
        pass  # The next timer retries; local status is always available.
    return {**status, 'server_commit': deployed, 'held_at_server': held, 'sync': report}


def status(home=Path.home()):
    path = state_dir(home)/'sync-status.json'
    return json.loads(path.read_text()) if path.exists() else None


def main():
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest='command',required=True)
    b=sub.add_parser('build'); b.add_argument('--output',type=Path,required=True)
    i=sub.add_parser('install'); i.add_argument('agent',choices=AGENTS); i.add_argument('--home',type=Path,default=Path.home())
    i.add_argument('--apply',action='store_true'); i.add_argument('--no-enable',action='store_true')
    i.add_argument('--hermes-command',default='hermes')
    s=sub.add_parser('sync',help='install every detected agent; stamp-skipped when unchanged')
    s.add_argument('--home',type=Path,default=Path.home())
    s.add_argument('--apply',action='store_true'); s.add_argument('--force',action='store_true')
    t=sub.add_parser('install-timer',help='run `update --apply` from a systemd --user timer or launchd agent')
    t.add_argument('--home',type=Path,default=Path.home())
    t.add_argument('--apply',action='store_true'); t.add_argument('--interval',type=int,default=15,metavar='MINUTES')
    u=sub.add_parser('update',help='pull this clone, never past the server commit, then sync')
    u.add_argument('--home',type=Path,default=Path.home()); u.add_argument('--apply',action='store_true')
    st=sub.add_parser('status',help='print the last timer sync result')
    st.add_argument('--home',type=Path,default=Path.home())
    en=sub.add_parser('enroll-host',help='mint and store this host claim credential; reuse it on later runs')
    en.add_argument('--home',type=Path,default=Path.home())
    args=p.parse_args()
    try:
        if args.command=='build':
            print(json.dumps({a:str(build(a,args.output/f'axon-{a}')) for a in AGENTS},indent=2))
        elif args.command=='enroll-host':
            print(json.dumps({'enrolled': True, 'path': str(enroll(args.home))}))
        elif args.command=='sync':
            report=sync(args.home,args.apply,args.force)
            print(json.dumps(report,indent=2))
            if report.get('failed'):
                # 'skipped' reports carry no failure map
                p.exit(2)
        elif args.command=='install-timer':
            print(json.dumps(install_timer(args.home,args.apply,args.interval),indent=2))
        elif args.command=='update':
            report=update(args.home,args.apply)
            print(json.dumps(report,indent=2))
            if report.get('ok') is False:
                p.exit(2)
        elif args.command=='status':
            current=status(args.home)
            print(json.dumps(current,indent=2))
            if current is None:
                p.exit(1,'No timer sync has run yet\n')
        else:
            print(json.dumps(install(args.agent,args.home,args.apply,not args.no_enable,args.hermes_command),indent=2))
    except (ValueError,OSError,subprocess.CalledProcessError) as error:
        p.exit(1,f'Axon install failed: {error}\nExisting configuration backups are under ~/.local/state/axon/install-backups.\n')


if __name__=='__main__':
    main()
