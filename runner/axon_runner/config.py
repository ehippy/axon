"""Host-local runner policy (#19): allowlist off by default, paths never leave the host.

The server only ever names projects; this module is the sole place a project
name becomes a folder path, and a job that matches nothing is refused."""
import tomllib
from pathlib import Path

DEFAULT_POLL = 55  # long-poll horizon; the server caps waits at 55s anyway


# Codex sandbox values the runner will pass; anything else (notably
# danger-full-access) is refused — the issue rule is "workspace-write with no
# bypass flags", and host config may only narrow from there.
SANDBOXES = ('read-only', 'workspace-write')
# Claude permission modes the runner will pass. bypassPermissions is never one:
# an unattended turn may only do what the host's own allow rules already permit.
PERMISSION_MODES = ('dontAsk', 'acceptEdits', 'plan', 'manual', 'auto')
RESUME_AGENTS = ('claude', 'codex')


class Policy:
    def __init__(self, server=None, allow=(), executable=None, worktree_root=None, resume=None):
        self.server = server
        self.allow = list(allow)
        # Tests and pilots point the agent name at a stub binary; production
        # leaves this unset and uses the agent's own CLI on PATH.
        self.executable = dict(executable or {})
        # Root for runner-managed git worktrees (agents without native worktree
        # support, e.g. Pi). Host-local, like every other path.
        self.worktree_root = worktree_root
        # Resuming sessions that ended (#18) is its own opt-in: None means off.
        self.resume = resume

    def resolve(self, agent, project):
        """Return (path, settings, refusal) for a job; settings carries the
        per-entry knobs each executor reads (permission_mode for Claude,
        sandbox for Codex). A request may only narrow host policy, never widen
        it, so matching is exact."""
        for entry in self.allow:
            if entry['project'] != project:
                continue
            if entry['agents'] and agent not in entry['agents']:
                continue
            path = Path(entry['path']).expanduser()
            if not path.is_dir():
                return None, None, 'project_path_missing'
            settings = {'permission_mode': entry['permission_mode'], 'sandbox': entry['sandbox'],
                        'worktree_root': self.worktree_root}
            return path, settings, None
        return None, None, 'not_allowed'


def load(path=None):
    """Read ~/.config/axon/runner.toml; a missing file means refuse everything."""
    path = Path(path or Path.home() / '.config/axon/runner.toml').expanduser()
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError:
        return Policy()
    allow = []
    for entry in raw.get('allow', []):
        if 'project' in entry and 'path' in entry:
            allow.append({'project': str(entry['project']), 'path': str(entry['path']),
                          'agents': [str(a) for a in entry.get('agents', [])],
                          'permission_mode': str(entry.get('permission_mode', 'dontAsk')),
                          'sandbox': str(entry.get('sandbox', 'workspace-write'))})
    resume = None
    if isinstance(raw.get('resume'), dict):
        entry = raw['resume']
        resume = {'agents': [a for a in (str(a) for a in entry.get('agents', RESUME_AGENTS)) if a in RESUME_AGENTS],
                  'permission_mode': str(entry.get('permission_mode', 'dontAsk')),
                  'sandbox': str(entry.get('sandbox', 'workspace-write'))}
    return Policy(server=raw.get('server'), allow=allow, executable=raw.get('executable', {}),
                  worktree_root=(str(Path(raw['worktree_root']).expanduser())
                                 if raw.get('worktree_root') else None), resume=resume)
