#!/usr/bin/env python3
"""Native pi lifecycle adapter; no server dependency."""
from pathlib import Path
import sys
root = Path(__file__).resolve().parents[1]
if (root / 'runtime').is_dir():
    sys.path.insert(0, str(root / 'runtime'))
else:
    for project in ('axon_protocol', 'axon_reporter'):
        sys.path.insert(0, str(root.parents[1] / 'packages/python' / project))
from axon_reporter.plugin_runtime import run_hook
if sys.argv[1:] == ['inbox']:
    from axon_reporter.native_inbox import main
    try:main('pi')
    except Exception:pass
else:
    run_hook('pi')
