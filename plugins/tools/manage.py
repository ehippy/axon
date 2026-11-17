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


# --- uninstall -----------------------------------------------------------
#
# `install` has five vendor-specific shapes, so removal is equally specific. The
# predicate stays the installer's own: a hook is ours only if its command runs
# `/.local/share/axon/plugins/…/hooks/run.py`, and a path is ours only if it is a
# symlink into our payload. Anything else in a host config is left byte-for-byte.

PAYLOAD_MARKER = '/.local/share/axon/'
AXON_ENV_PREFIX = 'AXON_'
OPT_IN_FLAG = 'auto-report'
# Written by the chezmoi run_after script; while it exists, that script treats the host
# as opted in even without the flag, because it predates the toggle.
DOTFILES_STAMP = 'hooks-installed'
# An older installer wrote a `description` beside `hooks`; that file is still ours.
OWNED_SETTINGS_KEYS = ('description',)


def axon_owned_extras(proposed):
    """True when every key beside `hooks` is one an Axon installer wrote itself."""
    extra = {k: v for k, v in proposed.items() if k != 'hooks'}
    if set(extra) - set(OWNED_SETTINGS_KEYS):
        return False
    return all(isinstance(v, str) and 'axon' in v.lower() for v in extra.values())


def axon_symlink(path, payload):
    """True for a symlink pointing inside our payload; a real file is never ours.

    Compared on resolved paths as well as the recorded text, because a host whose
    $HOME contains a symlink records targets that look different but point the same,
    and a dangling link has no realpath to fall back on.
    """
    if not path.is_symlink():
        return False
    target = Path(os.readlink(path))
    if not target.is_absolute():
        target = path.parent/target
    prefix = str(payload).replace('\\', '/').rstrip('/')
    return any(str(candidate).replace('\\', '/').rstrip('/') == prefix
               or str(candidate).replace('\\', '/').startswith(prefix+'/')
               for candidate in (target, target.resolve()))


def agent_links(home, agent):
    """Every symlink this agent's own config holds for Axon."""
    payload = home/'.local/share/axon/plugins'/agent
    if agent == 'hermes':
        links = [home/'.hermes/plugins/axon-activity',
                 *sorted((home/'.hermes/profiles').glob('*/plugins/axon-activity'))]
    elif agent == 'pi':
        links = [home/'.pi/agent/extensions/axon-activity', home/'.pi/agent/skills/axon-activity']
    elif agent == 'opencode':
        links = [home/'.config/opencode/plugins/axon-activity.js',
                 home/'.config/opencode/skills/axon-activity']
    else:
        links = [home/f'.{agent}'/'skills'/'axon-activity']
    return payload, links


def axon_statusline(value):
    """Claude's statusLine runs a payload binary when the resources panel set it."""
    return isinstance(value, dict) and \
        PAYLOAD_MARKER in str(value.get('command', '')).replace('\\', '/')


def codex_leftovers(home):
    """Codex files and trust-rule lines that name Axon but were never installed by us.

    A hand-written hook script and a sandbox rule granting Axon's reporter an allow
    decision outlive the hooks file, and neither is a symlink or a hook entry.
    """
    hooks_dir, rules_dir = home/'.codex'/'hooks', home/'.codex'/'rules'
    files = sorted(p for p in hooks_dir.iterdir() if 'axon' in p.name.lower()) \
        if hooks_dir.is_dir() else []
    rules = []
    for path in sorted(rules_dir.glob('*.rules')) if rules_dir.is_dir() else []:
        lines = path.read_text().splitlines()
        kept = [l for l in lines if PAYLOAD_MARKER not in l.replace('\\', '/')]
        if len(kept) != len(lines):
            rules.append((path, '\n'.join(kept)+'\n' if kept else '', len(lines)-len(kept)))
    return files, rules


def stripped_hooks(hooks):
    """Drop Axon handlers and the groups that held nothing else; return (hooks, removed)."""
    removed, result = 0, {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise ValueError(f'Invalid existing hook groups for {event}')
        kept = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get('hooks', []), list):
                raise ValueError(f'Invalid existing hook group for {event}')
            handlers = group['hooks']
            mine = [h for h in handlers if isinstance(h, dict) and ours(h.get('command', ''))]
            kept_handlers = [h for h in handlers if h not in mine]
            removed += len(handlers) - len(kept_handlers)
            if kept_handlers:
                kept.append({**group, 'hooks': kept_handlers})
            elif not handlers:
                # An empty group the host wrote itself is not ours to delete.
                kept.append(group)
        if kept:
            result[event] = kept
    return result, removed


def stripped_env(env):
    """Axon publishes transport through AXON_* keys; nothing else in env is ours."""
    kept = {k: v for k, v in env.items() if not str(k).startswith(AXON_ENV_PREFIX)}
    return kept, [k for k in env if k not in kept]


def usage_dirs(value, payload):
    """Remove only the Axon usage directory from Claude's CLAUDE_CODE_PLUGIN_DIRS."""
    parts = [d for d in str(value).split(os.pathsep) if d]
    marker = str(payload/'usage').replace('\\', '/').rstrip('/')
    kept = [d for d in parts if d.replace('\\', '/').rstrip('/') != marker]
    return os.pathsep.join(kept), len(parts) - len(kept)


def env_file_text(text):
    """Strip AXON_* assignments from a dotenv file, plus the `# …Axon…` comment line
    directly above them that the transport block wrote. Nothing else moves."""
    import re
    assignment = re.compile(r'\s*(?:export\s+)?AXON_[A-Z_]*\s*=')
    comment = re.compile(r'\s*#')
    mentions = re.compile(r'\baxon\b', re.IGNORECASE)
    lines = text.splitlines()
    drop = {i for i, line in enumerate(lines) if assignment.match(line)}
    for i in sorted(drop):
        above = i - 1
        while (above >= 0 and above not in drop and comment.match(lines[above])
               and mentions.search(lines[above])):
            drop.add(above)
            above -= 1
    kept = [line for i, line in enumerate(lines) if i not in drop]
    trailing = '\n' if text.endswith('\n') and kept else ''
    return '\n'.join(kept)+trailing, len(drop)


def toml_split_name(name):
    """Split a TOML table header into key parts, honouring quoted dotted keys."""
    parts, buf, quoted = [], '', False
    for char in name:
        if char == '"':
            quoted = not quoted
        elif char == '.' and not quoted:
            parts.append(buf)
            buf = ''
        else:
            buf += char
    return parts+[buf]


def toml_lookup(doc, parts):
    node = doc
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def toml_strip(text, drop):
    """Delete table header blocks `drop` accepts. Re-parses and proves the diff first.

    TOML has no comment model, so a `# Axon …` comment immediately above a removed
    table goes with it; nothing else is rewritten, and an unexpected diff raises.
    """
    import copy
    import tomllib
    before = tomllib.loads(text)
    lines = text.splitlines(keepends=True)
    headers = [(i, l.strip()[1:-1].strip()) for i, l in enumerate(lines)
               if l.lstrip().startswith('[') and not l.lstrip().startswith('[[')]
    remove, gone = set(), []
    explicit = {tuple(toml_split_name(header)) for _, header in headers}
    for n, (start, header) in enumerate(headers):
        end = headers[n+1][0] if n+1 < len(headers) else len(lines)
        parts = toml_split_name(header)
        value = toml_lookup(before, parts)
        if not drop(parts, value):
            continue
        if start > 0 and lines[start-1].lstrip().startswith('#') and 'axon' in lines[start-1].lower():
            remove.add(start-1)
        remove.update(range(start, end))
        gone.append((parts, header))
    if not gone:
        return text, []
    kept = ''.join(l for i, l in enumerate(lines) if i not in remove)
    kept = __import__('re').sub(r'\n{3,}', '\n\n', kept)
    after = tomllib.loads(kept)
    expected = copy.deepcopy(before)
    for parts, header in gone:
        parent = toml_lookup(expected, parts[:-1])
        if parent is not None:
            parent.pop(parts[-1], None)
        # A `[parent]` header written only to hold the removed table vanishes with it.
        # One the host wrote itself stays, even left empty: TOML distinguishes an
        # explicit empty table from an absent one, and so does the re-parse.
        for depth in range(len(parts)-1, 0, -1):
            node = toml_lookup(expected, parts[:depth])
            if node is None:
                continue
            if node or tuple(parts[:depth]) in explicit:
                break
            grand = toml_lookup(expected, parts[:depth-1])
            if isinstance(grand, dict):
                grand.pop(parts[depth-1], None)
    if after != expected:
        raise ValueError('TOML rewrite would change unrelated settings: '
                         +', '.join(header for _, header in gone))
    return kept, [header for _, header in gone]


def codex_config_text(text, hooks_path, hooks_empty):
    """Drop the Axon transport block, and the trust hashes of a hooks file we deleted."""
    def drop(parts, value):
        if parts == ['shell_environment_policy', 'set']:
            return isinstance(value, dict) and bool(value) and \
                all(str(k).startswith(AXON_ENV_PREFIX) for k in value)
        if parts[:2] == ['hooks', 'state'] and len(parts) == 3 and hooks_empty:
            recorded, colon, rest = str(parts[2]).partition(':')
            return bool(colon) and os.path.realpath(recorded) == os.path.realpath(str(hooks_path))
        return False
    return toml_strip(text, drop)


def uninstall(agent, home, apply=False, hermes_command='hermes', run=subprocess.run):
    """Undo one agent's Axon registration. Plans without apply; idempotent either way."""
    if agent not in AGENTS:
        raise ValueError('Unknown agent')
    home = Path(home).expanduser().resolve()
    payload, links = agent_links(home, agent)
    plan = {'agent': agent, 'apply': apply, 'settings': None, 'hooks_removed': 0,
            'env_removed': [], 'tables_removed': [], 'env_lines_removed': 0,
            'links_removed': [], 'settings_removed': None, 'writes': [], 'commands': [],
            'statusline_removed': False, 'files_removed': [], 'rules_lines_removed': 0}
    pending = []
    if agent in ('claude', 'codex'):
        settings = home/f'.{agent}'/('hooks.json' if agent == 'codex' else 'settings.json')
        plan['settings'] = str(settings)
        current = json.loads(settings.read_text()) if settings.exists() else None
        if current is not None and (not isinstance(current, dict)
                                    or not isinstance(current.get('hooks', {}), dict)):
            raise ValueError(f'{settings} must contain a hooks object')
        if current is None:
            hooks, removed = {}, 0
        else:
            hooks, removed = stripped_hooks(current['hooks'])
        plan['hooks_removed'] = removed
        proposed = {**current, 'hooks': hooks} if current is not None else None
        if agent == 'claude' and isinstance((proposed or {}).get('env'), dict):
            env, names = stripped_env(proposed['env'])
            plan['env_removed'] = names
            value, dropped = usage_dirs(env.get('CLAUDE_CODE_PLUGIN_DIRS', ''), payload)
            if dropped:
                if value:
                    env['CLAUDE_CODE_PLUGIN_DIRS'] = value
                else:
                    env.pop('CLAUDE_CODE_PLUGIN_DIRS', None)
                plan['env_removed'].append('CLAUDE_CODE_PLUGIN_DIRS')
            proposed['env'] = env
            if not env:
                proposed.pop('env')
        if proposed is not None and axon_statusline(proposed.get('statusLine')):
            # A statusline command naming our payload fails loudly once it is deleted.
            proposed.pop('statusLine')
            plan['statusline_removed'] = True
        if proposed == current:
            proposed = None
        elif not any(proposed['hooks'].values()) and axon_owned_extras(proposed):
            # Nothing but our hooks ever lived in this file, so the file is ours too.
            plan['settings_removed'] = str(settings)
            proposed = None
        if proposed is not None:
            pending.append((settings, json.dumps(proposed, indent=2)+'\n'))
        elif plan['settings_removed']:
            pending.append((settings, None))
        if agent == 'codex':
            config = home/'.codex'/'config.toml'
            if config.exists():
                original = config.read_text()
                text, gone = codex_config_text(original, home/'.codex'/'hooks.json',
                                               plan['settings_removed'] is not None)
                plan['tables_removed'] = gone
                if text != original:
                    pending.append((config, text))
            files, rules = codex_leftovers(home)
            plan['files_removed'] = [str(f) for f in files]
            for path, text, dropped in rules:
                plan['rules_lines_removed'] += dropped
                pending.append((path, text))
    elif agent == 'hermes':
        dotenv = home/'.hermes'/'.env'
        if dotenv.exists():
            original = dotenv.read_text()
            text, removed = env_file_text(original)
            plan['env_lines_removed'] = removed
            if text != original:
                pending.append((dotenv, text))
    plan['writes'] = [str(path) for path, _ in pending]
    for link in links:
        if axon_symlink(link, payload):
            plan['links_removed'].append(str(link))
    if agent == 'hermes':
        plan['commands'] = [f'{hermes_command} plugins disable axon-activity']
    if not apply:
        return plan

    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    backup = state_dir(home)/'config-backups'/f'pre-uninstall-{stamp}'
    backup.mkdir(parents=True, mode=0o700)
    plan['backup'] = str(backup)
    for path, text in pending:
        if path.exists():
            shutil.copy2(path, backup/path.name)
        if text is None:
            path.unlink()
        elif path.suffix == '.json':
            write_json(path, json.loads(text))
        else:
            mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
            path.write_text(text)
            path.chmod(mode)
    for name in plan['links_removed']:
        Path(name).unlink()
    for name in plan['files_removed']:
        path = Path(name)
        if path.is_file():
            shutil.copy2(path, backup/path.name)
            path.unlink()
    if agent == 'hermes':
        search_path = os.environ.get('PATH', '')+os.pathsep+os.pathsep.join(
            str(home/d) for d in EXTRA_BIN_DIRS)
        executable = shutil.which(hermes_command, path=search_path)
        if executable:
            # Hermes owns its plugin registry; ask it before the payload disappears.
            plan['commands'] = [shlex.join([executable, 'plugins', 'disable', 'axon-activity'])]
            try:
                run([executable, 'plugins', 'disable', 'axon-activity'], check=False,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass
    return plan


def launchd_axon_agents(home):
    """LaunchAgents owned by this user that run Axon code (label alone is not proof).

    `org.axon.sync` is excluded: `uninstall_timer` owns the installer's own unit.
    """
    import plistlib
    found = {}
    for plist in sorted((home/'Library'/'LaunchAgents').glob('org.axon.*.plist')):
        try:
            body = plistlib.loads(plist.read_bytes())
        except (ValueError, OSError):
            continue
        label = body.get('Label', plist.stem)
        if label == 'org.axon.sync':
            continue
        program = ' '.join(body.get('ProgramArguments', [])).replace('\\', '/')
        if '.local/share/axon/' in program or 'axon-runner' in program or 'axon-resources' in program:
            found[label] = plist
    return found


def systemd_axon_units(home):
    """User units written by `install_timer`; anything else is reported, not stopped."""
    units = home/'.config'/'systemd'/'user'
    owned = {name: units/name for name in ('axon-sync.service', 'axon-sync.timer')
             if (units/name).exists()}
    other = sorted(p.name for p in units.glob('axon-*') if p.name not in owned) if units.exists() else []
    return owned, other


def uninstall_timer(home, apply=False, system=None, run=subprocess.run):
    """Remove the timer that would reinstall everything on the next tick."""
    home = Path(home).expanduser()
    system = system or platform.system()
    plan = {'system': system, 'files': [], 'commands': [], 'apply': apply}
    if system == 'Darwin':
        plist = home/'Library'/'LaunchAgents'/'org.axon.sync.plist'
        commands = [['launchctl', 'bootout', f'gui/{os.getuid()}/org.axon.sync']]
        files = [plist] if plist.exists() else []
    elif system == 'Linux':
        units = home/'.config'/'systemd'/'user'
        files = [units/n for n in ('axon-sync.service', 'axon-sync.timer') if (units/n).exists()]
        commands = [['systemctl', '--user', 'disable', '--now', 'axon-sync.timer', 'axon-sync.service'],
                    ['systemctl', '--user', 'daemon-reload']]
    else:
        return plan
    plan['files'] = [str(p) for p in files]
    plan['commands'] = [shlex.join(c) for c in commands]
    if not apply:
        return plan
    for command in commands:
        run(command, check=False, capture_output=True)
    for path in files:
        path.unlink()
    return plan


def uninstall_services(home, apply=False, system=None, run=subprocess.run):
    """Stop and remove Axon's companion jobs and tools that `install` never owned."""
    home = Path(home).expanduser()
    system = system or platform.system()
    plan = {'jobs': [], 'plists': [], 'uv_tools': [], 'commands': [], 'manual_units': [], 'apply': apply}
    if system == 'Darwin':
        agents = launchd_axon_agents(home)
        plan['jobs'] = sorted(agents)
        plan['plists'] = [str(p) for p in agents.values()]
        if apply:
            for label, plist in agents.items():
                run(['launchctl', 'bootout', f'gui/{os.getuid()}/{label}'], check=False,
                    capture_output=True)
                plist.unlink()
    else:
        owned, other = systemd_axon_units(home)
        plan['plists'] = [str(p) for p in owned.values()]
        plan['manual_units'] = other
        plan['commands'] = [['systemctl', '--user', 'daemon-reload']]
        if apply and owned:
            run(['systemctl', '--user', 'disable', '--now', *sorted(p.name for p in owned.values())],
                check=False, capture_output=True)
            for path in owned.values():
                path.unlink()
            run(['systemctl', '--user', 'daemon-reload'], check=False, capture_output=True)
    if shutil.which('uv'):
        done = run(['uv', 'tool', 'list'], check=False, capture_output=True, text=True)
        tools = str(getattr(done, 'stdout', '') or '')
        installed = [t for t in ('axon-runner',) if f'{t} v' in tools]
        plan['uv_tools'] = installed
        if apply:
            for tool in installed:
                run(['uv', 'tool', 'uninstall', tool], check=False, capture_output=True)
                link = home/'.local'/'bin'/tool
                if link.is_symlink() and '.local/share/uv/tools/' in os.readlink(link):
                    link.unlink()
    return plan


def archive_host(home, destination, stamp=None):
    """Copy what identifies this host, and its journal, before anything is deleted."""
    destination = Path(destination).expanduser().resolve()
    home = Path(home).expanduser()
    label = read_machine_label(machine_label_path(home)) or platform.node()
    label = ''.join(c if c.isalnum() or c in '-_' else '-' for c in str(label)) or 'host'
    target = destination/f'axon-{label}-{stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")}'
    target.mkdir(parents=True)
    target.chmod(0o700)
    state = state_dir(home)
    copied = []
    config = home/'.config'/'axon'
    if config.exists():
        shutil.copytree(config, target/'config-axon')
        copied.append('config-axon')
    for path in sorted(state.glob('*.sqlite3'))+sorted(state.glob('*.json')):
        shutil.copy2(path, target/path.name)
        copied.append(path.name)
    backup = state/'config-backups'
    if backup.exists():
        shutil.copytree(backup, target/'config-backups', symlinks=True)
        copied.append('config-backups')
    for path in [target, *target.rglob('*')]:
        path.chmod(0o700 if path.is_dir() else 0o600)
    return {'path': str(target), 'copied': copied}


def purge_host(home, archive=None, discard=False, i_am_server=False):
    """Delete payload and state. Refuses to guess what happens to the host's data."""
    home = Path(home).expanduser()
    if archive and discard:
        raise ValueError('Choose --archive DIR or --discard-data, not both')
    if not archive and not discard:
        raise ValueError('Purging needs --archive DIR or --discard-data; nothing was deleted')
    payload, state = home/'.local/share/axon', state_dir(home)
    if (payload/'server').exists() and not i_am_server:
        raise ValueError(f'{payload}/server exists: this host runs the Axon journal server and '
                         'other hosts report to it. See docs/DECOMMISSION.md, then pass '
                         '--i-am-the-server once the server and its timers are stopped.')
    saved = archive_host(home, archive) if archive else None
    for path in (payload, state, home/'.config'/'axon'):
        shutil.rmtree(path, ignore_errors=True)
    return {'removed': [str(payload), str(state), str(home/'.config'/'axon')], 'archived': saved}


def uninstall_agents(home, agents, apply=False):
    reports, failed = {}, {}
    for agent in agents:
        try:
            reports[agent] = uninstall(agent, home, apply)
        except Exception as failure:  # noqa: BLE001 — one malformed agent must not stop the rest
            failed[agent] = str(failure)
    return reports, failed


def decommission(home=Path.home(), apply=False, purge=False, archive=None, discard=False,
                 i_am_server=False, agent=None, system=None, run=subprocess.run):
    """Take one host out of Axon: unregister agents, stop the timers, then optionally purge.

    Every agent is visited, not just the detected ones: a hook left behind by an agent
    whose binary is off PATH now still fires the next time that agent runs.
    """
    home = Path(home).expanduser().resolve()
    agents = (agent,) if agent else AGENTS
    report = {'home': str(home), 'apply': apply, 'agents': {}, 'failed': {},
              'payload': str(home/'.local/share/axon'), 'journal': str(state_dir(home))}
    if apply:
        # Clear both opt-in markers first: a concurrent `chezmoi apply` must not
        # re-install mid-run, and the stamp alone is enough for it to think it may.
        for marker in (home/'.config'/'axon'/OPT_IN_FLAG, state_dir(home)/DOTFILES_STAMP):
            if marker.exists():
                marker.unlink()
                report.setdefault('opt_in', []).append(str(marker))
        report['agents'], report['failed'] = uninstall_agents(home, agents, True)
        report['timer'] = uninstall_timer(home, True, system, run)
        report['services'] = uninstall_services(home, True, system, run)
        if purge:
            report['purge'] = purge_host(home, archive, discard, i_am_server)
    else:
        report['agents'], report['failed'] = uninstall_agents(home, agents, False)
        report['timer'] = uninstall_timer(home, False, system, run)
        if (system or platform.system()) == 'Darwin':
            jobs = launchd_axon_agents(home)
            units = []
        else:
            jobs = {}
            _, units = systemd_axon_units(home)
        report['services'] = {'jobs': sorted(jobs), 'plists': [str(p) for p in jobs.values()],
                              'manual_units': units}
        report['purge'] = (purge_plan(home, archive, discard, i_am_server) if purge
                           else {'planned': False})
    report['cleared'] = sorted(a for a, r in report['agents'].items()
                               if isinstance(r, dict) and any(
                                   r.get(k) for k in ('hooks_removed', 'env_removed', 'tables_removed',
                                                      'env_lines_removed', 'links_removed', 'settings_removed',
                                                      'statusline_removed', 'files_removed',
                                                      'rules_lines_removed')))
    report['manual_steps'] = []
    if not purge:
        report['manual_steps'].append(
            f'journal and payload left in place; rerun with --purge --archive DIR or --discard-data '
            f'to remove {report["payload"]}, {report["journal"]} and ~/.config/axon')
    report['manual_steps'] += [f'review systemd unit {u}: it was not written by install-timer'
                               for u in report['services'].get('manual_units', [])]
    report['manual_steps'].append('reload running agent sessions: an open session still carries the '
                                  'reporting instructions in its context')
    if report['failed']:
        report['warning'] = 'these agents were left installed; repair the config and rerun'
    return report


def purge_plan(home, archive=None, discard=False, i_am_server=False):
    """Dry-run purge: report what would be archived and deleted, deleting nothing."""
    home = Path(home).expanduser()
    would = [str(home/'.local/share/axon'), str(state_dir(home)), str(home/'.config'/'axon')]
    guarded = (home/'.local/share/axon'/'server').exists() and not i_am_server
    return {'planned': True, 'would_remove': would,
            'archive': str(archive) if archive else None,
            'discard': bool(discard),
            'blocked_reason': ('this host runs the Axon journal server'
                               if guarded else (None if archive or discard else
                                               'purge needs --archive DIR or --discard-data'))}


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
    x=sub.add_parser('uninstall',help='remove Axon from this host: agent hooks, timers, then optionally payload and journal')
    x.add_argument('agent',nargs='?',default='all',choices=[*AGENTS,'all'])
    x.add_argument('--home',type=Path,default=Path.home())
    x.add_argument('--apply',action='store_true')
    x.add_argument('--purge',action='store_true',help='also delete the payload, journal and ~/.config/axon')
    x.add_argument('--archive',type=Path,metavar='DIR',help='copy credentials and journal here before --purge deletes them')
    x.add_argument('--discard-data',action='store_true',help='delete credentials and journal with no copy')
    x.add_argument('--i-am-the-server',action='store_true',help='acknowledge this host is the journal other hosts report to')
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
        elif args.command=='uninstall':
            report=decommission(args.home,args.apply,args.purge,args.archive,args.discard_data,
                                args.i_am_the_server,
                                None if args.agent=='all' else args.agent)
            print(json.dumps(report,indent=2))
            if report.get('failed'):
                p.exit(2)
        else:
            print(json.dumps(install(args.agent,args.home,args.apply,not args.no_enable,args.hermes_command),indent=2))
    except (ValueError,OSError,subprocess.CalledProcessError) as error:
        p.exit(1,f'Axon install failed: {error}\nExisting configuration backups are under ~/.local/state/axon/install-backups.\n')


if __name__=='__main__':
    main()
