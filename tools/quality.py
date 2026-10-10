# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta
"""Reproducible offline checks. Reports remain local and contain no raw test output."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

from tools.consistency import digest
from tools.install_skill import LocalPaths

MUTATIONS = (
    ('validation.audit_mutation_checks', 7), ('validation.audit_new_payee_checks', 4),
    ('validation.audit_transaction_read_checks', 10), ('validation.audit_budget_planning_checks', 7),
)


def mutation_result(output, expected):
    try:
        rows = [json.loads(line) for line in output.splitlines() if line.strip()]
        control = rows[0]['control']
        faults = rows[1:]
        valid = (control['tests'] > 0 and not control.get('errors')
                 and not control.get('failures') and not control.get('failure_count'))
        valid = valid and len(faults) == expected and len({r['fault'] for r in faults}) == expected
        for fault in faults:
            valid = valid and bool(fault.get('detected', fault.get('combined_detected', False)))
            valid = valid and not fault.get('errors') and fault.get('exercised', True)
            for key in ('baseline_suite', 'added_contract_suite'):
                valid = valid and not fault.get(key, {}).get('errors')
        return {'passed': bool(valid), 'control': control, 'faults': faults}
    except (ValueError, KeyError, IndexError, TypeError):
        return {'passed': False, 'reason': 'Missing or invalid structured mutation results'}


def current(report, source_digest, required_mode=None):
    checks = report.get('checks', [])
    expected = 7 if report.get('mode') == 'full' else 3
    return ((required_mode is None or report.get('mode') == required_mode)
            and report.get('source_digest') == source_digest and report.get('status') == 'passed'
            and len(checks) == expected and all(r.get('passed') and r.get('exit_code') == 0
            and r.get('terminal_status') == 'completed' for r in checks))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('quick', 'full'))
    parser.add_argument('--check-report', metavar='LOCAL_REPORT')
    args = parser.parse_args()
    root = Path(__file__).absolute().parents[1]
    safe = LocalPaths()
    safe.check(root)
    safe.check('/private/tmp')
    before = digest(root)
    if args.check_report:
        path = Path(os.path.abspath(args.check_report))
        if path.parent != root / 'tmp/quality':
            raise SystemExit('Select a local report under this checkout tmp/quality directory')
        report = json.loads(safe.read(path))
        valid = current(report, before, args.mode)
        print('Report is current and passed' if valid else 'Report is stale or not passed')
        return 0 if valid else 1
    report = {'schema_version': 1, 'mode': args.mode, 'started_at': datetime.now(timezone.utc).isoformat(),
              'source_digest': before, 'python': platform.python_version(),
              'platform': {'system': platform.system(), 'release': platform.release(), 'machine': platform.machine()},
              'checks': [], 'status': 'incomplete'}
    git = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, text=True, capture_output=True)
    report['revision'] = git.stdout.strip() if git.returncode == 0 else 'uncommitted'
    checks = [('tools.checks', 'consistency'), ('tools.checks', 'core'), ('tools.checks', 'safety')]
    if args.mode == 'full':
        checks.extend((module, None) for module, _ in MUTATIONS)
    with tempfile.TemporaryDirectory(prefix='ynab-offline-', dir='/private/tmp') as temporary:
        env = os.environ.copy()
        env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONPYCACHEPREFIX=temporary + '/pycache', TMPDIR=temporary,
                   YNAB_TEST_SOURCE=str(root), YNAB_TEST_TEMP=temporary, YNAB_TEST_HOME=str(Path.home()),
                   PYTHONPATH=str(root / 'tools/offline_guard') + os.pathsep + str(root))
        for module, kind in checks:
            command = [sys.executable, '-B', '-m', module] + ([kind] if kind else [])
            print('Checking ' + (kind or module) + '...', flush=True)
            try:
                completed = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True, timeout=600)
                if kind:
                    try:
                        result = json.loads(completed.stdout)
                    except ValueError:
                        result = {'passed': False, 'reason': 'Check did not produce structured results'}
                        print(completed.stderr)
                else:
                    result = mutation_result(completed.stdout, dict(MUTATIONS)[module])
                result['passed'] = result.get('passed', False) and completed.returncode == 0
                result.update(command=['python3', '-B', '-m', module] + ([kind] if kind else []),
                              exit_code=completed.returncode, terminal_status='completed')
            except subprocess.TimeoutExpired:
                result = {'passed': False, 'command': ['python3', '-B', '-m', module], 'exit_code': None, 'terminal_status': 'timed_out'}
            report['checks'].append(result)
            print('  ' + ('passed' if result['passed'] else 'FAILED'), flush=True)
    report['status'] = 'passed' if all(r['passed'] for r in report['checks']) and before == digest(root) else 'failed'
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    # Fixed local output only, excluded from Git and the source digest.
    output = safe.check(root / 'tmp/quality')
    if output.is_symlink() or output.parent.is_symlink():
        raise SystemExit('Refusing linked report directory')
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = output / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.json')
    with name.open('x') as stream:
        json.dump(report, stream, indent=2)
    name.chmod(0o600)
    print('Report: ' + str(name.relative_to(root)))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    sys.exit(main())
