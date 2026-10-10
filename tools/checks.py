# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta
"""Structured child checks for tools.quality; no live financial operations."""
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest


def suite(directory):
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
        result = unittest.TextTestRunner(stream=stream, verbosity=0).run(
            unittest.TestLoader().discover(directory))
    return {'tests': result.testsRun, 'failures': len(result.failures),
            'errors': len(result.errors), 'failed_cases': [t.id() for t, _ in result.failures + result.errors], 'skipped': len(result.skipped),
            'passed': result.wasSuccessful() and result.testsRun > 0 and not result.skipped}


def main():
    if not getattr(sys, '_ynab_offline_guard', False):
        raise SystemExit('Run through python3 -B -m tools.quality so the offline guard is active')
    kind = sys.argv[1]
    if kind == 'consistency':
        from tools.consistency import check
        errors = check(Path.cwd())
        result = {'passed': not errors, 'issues': errors}
    else:
        result = suite('tests' if kind == 'core' else 'validation')
    print(json.dumps(result))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
