#!/usr/bin/env python3
"""Reject private IPs and local hostnames in source and release text artifacts."""
import argparse
import io
import ipaddress
from pathlib import Path
import re
import subprocess
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
IP = re.compile(rb'\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b')
HOST = re.compile(rb'\b[a-zA-Z0-9][a-zA-Z0-9-]*\.local\b')


def check(name, data):
    if b'\0' in data:
        return
    for value in IP.findall(data):
        try:
            address = ipaddress.ip_address(value.decode())
        except ValueError:
            continue
        if not address.is_loopback and not address.is_unspecified and (
                address.is_private or address in ipaddress.ip_network(((100 << 24) | (64 << 16), 10))):
            raise ValueError(f'Private network address in {name}')
    if HOST.search(data):
        raise ValueError(f'Local hostname in {name}')


def scan_release(release):
    for path in release.rglob('*'):
        if not path.is_file(): continue
        if path.suffix == '.whl':
            with zipfile.ZipFile(path) as archive:
                for name in archive.namelist(): check(f'{path.name}:{name}',archive.read(name))
        elif path.name.endswith('.tar.gz'):
            with tarfile.open(path) as archive:
                for member in archive:
                    if member.isfile(): check(f'{path.name}:{member.name}',archive.extractfile(member).read())
        else: check(str(path),path.read_bytes())

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release',type=Path)
    args=parser.parse_args()
    paths=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=ROOT).decode().split('\0')
    for name in filter(None,paths):
        path=ROOT/name
        if path.is_file():check(name,path.read_bytes())
    if args.release:scan_release(args.release)
    print('Source/release privacy pattern checks passed; use a secret scanner as well')
