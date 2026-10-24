#!/usr/bin/env python3
"""Build self-contained Axon plugins or install them without replacing unrelated hooks."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

SOFTWARE = Path(__file__).resolve().parents[1]
AGENTS = ('codex','claude','hermes')


def build(agent, destination):
    source = SOFTWARE/f'{agent}-hooks'
    destination = Path(destination)
    # Destination is a dedicated package directory; don't copy caches or legacy gateways.
    destination.mkdir(parents=True,exist_ok=True)
    selected = ['README.md','__init__.py','plugin.yaml'] if agent == 'hermes' else ['README.md','hooks','skills', 'plugin.json' if agent=='codex' else '.claude-plugin']
    for name in selected:
        src, dst = source/name, destination/name
        if src.is_dir():
            shutil.copytree(src,dst,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__'))
        else:
            shutil.copy2(src,dst)
    runtime=destination/'runtime'
    runtime.mkdir(exist_ok=True)
    for name in ('__init__.py','report.py','journal.py','query.py','plugin_runtime.py'):
        shutil.copy2(SOFTWARE/'activity-journal'/name,runtime/name)
    return destination


def ours(command):
    try:
        words=shlex.split(command)
    except (ValueError,TypeError):
        return False
    for word in words:
        path=word.replace('\\','/')
        if path.endswith('/.codex/hooks/codex_axon.py') or path.endswith('/glass/hooks/codex_axon.py'):
            return True
        if '/axon/' in path and any(path.endswith(f'/{a}-hooks/report_status.py') for a in ('codex','claude')):
            return True
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
    bundled=json.loads((SOFTWARE/f'{agent}-hooks/hooks/hooks.json').read_text())['hooks']
    command=shlex.join([sys.executable,str(package/'hooks/run.py')])
    for event, groups in bundled.items():
        for group in groups:
            for handler in group['hooks']:
                handler['command']=command
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
    settings=home/f'.{agent}'/('hooks.json' if agent=='codex' else 'settings.json' if agent=='claude' else 'config.yaml')
    # Read malformed configuration before making any changes.
    current=json.loads(settings.read_text()) if agent!='hermes' and settings.exists() else {}
    if agent!='hermes' and (not isinstance(current,dict) or not isinstance(current.get('hooks',{}),dict)):
        raise ValueError('Invalid existing settings; nothing changed')
    proposed=merged_config(current,agent,package) if agent!='hermes' else None
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
            legacy=home/'.hermes/hooks/axon-status/HOOK.yaml'
            if legacy.exists():
                legacy.rename(backup/'legacy-HOOK.yaml')
    else:
        write_json(settings,proposed)
        skill=home/f'.{agent}/skills/axon-activity'
        skill.parent.mkdir(parents=True,exist_ok=True)
        if skill.exists() or skill.is_symlink():
            skill.rename(backup/'skill')
        skill.symlink_to(package/'skills/axon-activity',target_is_directory=True)
    return {**plan,'backup':str(backup),'restart_required':True,
            'hook_review_required':agent=='codex'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest='command',required=True)
    b=sub.add_parser('build'); b.add_argument('--output',type=Path,required=True)
    i=sub.add_parser('install'); i.add_argument('agent',choices=AGENTS); i.add_argument('--home',type=Path,default=Path.home())
    i.add_argument('--apply',action='store_true'); i.add_argument('--no-enable',action='store_true')
    i.add_argument('--hermes-command',default='hermes')
    args=p.parse_args()
    try:
        if args.command=='build':
            print(json.dumps({a:str(build(a,args.output/f'axon-{a}')) for a in AGENTS},indent=2))
        else:
            print(json.dumps(install(args.agent,args.home,args.apply,not args.no_enable,args.hermes_command),indent=2))
    except (ValueError,OSError,subprocess.CalledProcessError) as error:
        p.exit(1,f'Axon install failed: {error}\nExisting configuration backups are under ~/.local/state/axon/install-backups.\n')


if __name__=='__main__':
    main()
