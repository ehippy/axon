#!/usr/bin/env python3
"""Run all Python component and protocol checks without installing live hooks."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
suite = unittest.TestSuite()
for path in ('protocol/tests', 'server/tests', 'plugins/tests', 'runner/tests', 'tests'):
    suite.addTests(unittest.TestLoader().discover(str(ROOT/path)))
for path in sorted((ROOT/'plugins').glob('*/tests')):
    suite.addTests(unittest.TestLoader().discover(str(path)))
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(not result.wasSuccessful())
