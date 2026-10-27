#!/usr/bin/env python3
"""Build independent Python wheels and standalone plugin archives, without publishing."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = '0.3.0'
PROJECTS = ('packages/python/axon_protocol', 'packages/python/axon_reporter', 'server', 'clients/query')


def build(output, handset_binary=None):
    output = Path(output).resolve()
    # Avoid stale artifacts or accidentally overwriting unrelated user files.
    if output.exists() and any(output.iterdir()):
        raise ValueError('Output must be empty; choose a new release directory')
    output.mkdir(parents=True, exist_ok=True)
    wheels = output/'wheels'
    wheels.mkdir()
    spec = importlib.util.spec_from_file_location('axon_package_builder', ROOT/'plugins/tools/manage.py')
    manager = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(manager)
    with tempfile.TemporaryDirectory(prefix='axon-release-') as tmp:
        tmp = Path(tmp)
        for project in PROJECTS:
            source = tmp/Path(project).name
            shutil.copytree(ROOT/project, source, ignore=shutil.ignore_patterns('__pycache__', 'build', '*.egg-info'))
            subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation',
                            '--wheel-dir', str(wheels), str(source)], check=True)
        for agent in manager.AGENTS:
            name = f'axon-{agent}'
            package = manager.build(agent, tmp/name)
            with tarfile.open(output/f'{name}-{VERSION}.tar.gz', 'w:gz') as archive:
                archive.add(package, arcname=name, filter=lambda info: _portable(info))
    if handset_binary:
        binary = Path(handset_binary)
        if not binary.is_file():
            raise ValueError('Handset binary does not exist')
        shutil.copy2(binary, output/'axon-native-linux-aarch64')
    manifest = {'release':VERSION, 'protocol':1, 'components':['server','reporter','query','handset','plugins'],
                'handset_binary_included':bool(handset_binary), 'files':{}}
    for path in sorted(output.rglob('*')):
        if path.is_file():
            manifest['files'][str(path.relative_to(output))] = hashlib.sha256(path.read_bytes()).hexdigest()
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    return manifest


def _portable(info):
    info.uid = info.gid = 0
    info.uname = info.gname = ''
    info.mtime = 0
    return info


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--handset-binary', type=Path, help='Verified Linux aarch64 axon-native executable')
    args = p.parse_args()
    build(args.output, args.handset_binary)

if __name__ == '__main__': main()
