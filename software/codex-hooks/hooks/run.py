#!/usr/bin/env python3
"""Axon codex plugin hook: private lifecycle facts and session-scoped guidance."""
from pathlib import Path
import sys
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / 'runtime' if (root / 'runtime').is_dir() else root.parent / 'activity-journal'))
from plugin_runtime import run_hook
run_hook('codex')
