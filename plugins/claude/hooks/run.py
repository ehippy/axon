#!/usr/bin/env python3
"""Native claude lifecycle adapter; no server dependency."""
from pathlib import Path
import sys
root = Path(__file__).resolve().parents[1]
if (root / 'runtime').is_dir():
    sys.path.insert(0, str(root / 'runtime'))
else:
    for project in ('axon_protocol', 'axon_reporter'):
        sys.path.insert(0, str(root.parents[1] / 'packages/python' / project))
from axon_reporter.plugin_runtime import run_hook
run_hook('claude')
