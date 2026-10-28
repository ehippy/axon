#!/usr/bin/env python3
"""Build self-contained Axon plugins or install them without replacing unrelated hooks."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
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
        for name in selected:
            src, dst = source/name, staged/name
            if src.is_dir():
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns('__pycache__'))
            else:
                shutil.copy2(src, dst)
        for name in ('axon_protocol', 'axon_reporter'):
            shutil.copytree(ROOT/'packages/python'/name/name, staged/'runtime'/name,
                            ignore=shutil.ignore_patterns('__pycache__'))
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
    return result


def write_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.axon-tmp')
    tmp.write_text(json.dumps(value,indent=2)+'\n')
    tmp.chmod(path.stat().st_mode & 0o777 if path.exists() else 0o600)
    tmp.replace(path)


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
    plan={'agent':agent,'package':str(package),'settings':str(settings),'apply':apply}
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
            subprocess.run([hermes_command,'plugins','enable','axon-activity'],check=True)
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
    return {**plan,'backup':str(backup),'restart_required':True,
            'hook_review_required':agent=='codex'}


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
    if not force and agents and stamp.exists() and stamp.read_text() == want:
        return {'revision': revision, 'agents': agents, 'skipped': True}
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
            'installed': installed, 'failed': failed, 'skipped': False}


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
    args=p.parse_args()
    try:
        if args.command=='build':
            print(json.dumps({a:str(build(a,args.output/f'axon-{a}')) for a in AGENTS},indent=2))
        elif args.command=='sync':
            report=sync(args.home,args.apply,args.force)
            print(json.dumps(report,indent=2))
            if report['failed']:
                p.exit(2)
        else:
            print(json.dumps(install(args.agent,args.home,args.apply,not args.no_enable,args.hermes_command),indent=2))
    except (ValueError,OSError,subprocess.CalledProcessError) as error:
        p.exit(1,f'Axon install failed: {error}\nExisting configuration backups are under ~/.local/state/axon/install-backups.\n')


if __name__=='__main__':
    main()
