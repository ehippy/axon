"""Host-local inbox credential storage. Dashboard input tokens are never persisted here."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.request

from . import report


def credential_path(home=None):
    configured = report.setting('AXON_HOST_CREDENTIAL_FILE')
    return Path(configured).expanduser() if configured else Path(home or Path.home())/'.config/axon/host-credential'


def read_credential():
    try:
        return credential_path().read_text().strip()
    except FileNotFoundError:
        return None


def enroll(home=None):
    """Mint once using the broker's input token in place, or an explicit local token file."""
    path = credential_path(home)
    if path.exists():
        path.chmod(0o600)
        return path
    machine = report.machine_identity()
    payload = json.dumps({'machine': machine}).encode()
    token_file = report.setting('AXON_INPUT_TOKEN_FILE')
    if token_file:
        token = Path(token_file).expanduser().read_text().strip()
        url = report.setting('AXON_SERVER') or 'http://%s:8510' % report.setting('AXON_BROKER', 'localhost')
        request = urllib.request.Request(url.rstrip('/')+'/hosts/credentials', data=payload,
            headers={'Authorization': 'Bearer '+token, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.load(response)
    else:
        broker = report.setting('AXON_BROKER')
        if not broker or broker.startswith('-'):
            raise ValueError('Host enrollment needs AXON_BROKER or AXON_INPUT_TOKEN_FILE')
        # Only the enrollment result crosses SSH; the input token remains on the broker.
        command = 'curl --fail --silent --show-error --max-time 10 -X POST -H "Authorization: Bearer $(cat ~/.config/axon/input-token)" -H "Content-Type: application/json" --data-binary @- http://127.0.0.1:8510/hosts/credentials'
        completed = subprocess.run(['ssh', '-o', 'BatchMode=yes', '--', broker, command],
            input=payload, capture_output=True, timeout=20, check=True)
        result = json.loads(completed.stdout)
    credential = result.get('credential')
    if result.get('machine') != machine or not isinstance(credential, str) or len(credential) < 32:
        raise ValueError('Invalid host enrollment response')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Publish only a complete file, without replacing a credential from another sync.
    fd, staged = tempfile.mkstemp(prefix='.host-credential-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as target:
            target.write(credential+'\n')
        try:
            os.link(staged, path)
        except FileExistsError:
            pass
    finally:
        os.unlink(staged)
    return path
