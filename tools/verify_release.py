#!/usr/bin/env python3
"""Verify checksums and install each Python component without the source tree."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import venv


def verify(release):
    release = Path(release).resolve()
    manifest = json.loads((release/'manifest.json').read_text())
    for name, expected in manifest['files'].items():
        path = release/name
        if not path.resolve().is_relative_to(release):
            raise ValueError('Artifact path escapes release directory')
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f'Checksum mismatch: {name}')
    env = {k:v for k,v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME')}
    with tempfile.TemporaryDirectory(prefix='axon-install-check-') as tmp:
        tmp = Path(tmp)
        env.update(AXON_ACTIVITY_HOME=str(tmp/'state'), AXON_MACHINE='release-check',
                   AXON_MOSQUITTO_PUB='/missing/axon-pub', PYTHONDONTWRITEBYTECODE='1')
        for component, entry, forbidden in (
                ('server','axon-server', ('axon_reporter', 'axon_query')),
                ('reporter','axon-report', ('axon_server', 'axon_query')),
                ('query','axon-query', ('axon_server', 'axon_reporter', 'axon_protocol'))):
            target = tmp/component
            venv.EnvBuilder(with_pip=True).create(target)
            executable = target/'bin/python'
            subprocess.run([str(executable),'-m','pip','install','--no-index','--find-links',str(release/'wheels'),f'axon-{component}'],cwd=tmp,env=env,check=True)
            script = 'import importlib.util; assert all(importlib.util.find_spec(n) is None for n in '+repr(forbidden)+')'
            subprocess.run([str(executable),'-c',script],cwd=tmp,env=env,check=True)
            subprocess.run([str(target/'bin'/entry),'--help'],cwd=tmp,env=env,check=True,stdout=subprocess.DEVNULL)
            if component == 'reporter':
                subprocess.run([str(target/'bin/axon-resources'),'--help'],cwd=tmp,env=env,check=True,stdout=subprocess.DEVNULL)
                result = subprocess.run([str(target/'bin'/entry),'--session','release-chat','start','--goal','Verify install'],cwd=tmp,env=env,check=True,capture_output=True,text=True)
                assert json.loads(result.stdout)['queued']
            if component == 'server':
                script = "from importlib.resources import files; root=files('axon_server').joinpath('web'); assert all(root.joinpath(name).read_bytes() for name in ('index.html','app.js','app.css','favicon.svg'))"
                subprocess.run([str(executable),'-c',script],cwd=tmp,env=env,check=True)
            print(f'Independent {component} installation passed', flush=True)
    print('Release checksums and independent installs passed')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('release',type=Path)
    verify(parser.parse_args().release)
