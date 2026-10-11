# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools import consistency, quality


class QualityTests(unittest.TestCase):
    def test_mutation_results_reject_missing_faults_bad_control_and_errors(self):
        control = {'control': {'tests': 1, 'errors': 0, 'failures': 0}}
        fault = {'fault': 'synthetic', 'tests': 1, 'errors': 0, 'detected': True, 'exercised': True}
        def check(c, f, expected=1):
            return quality.mutation_result(json.dumps(c) + '\n' + json.dumps(f), expected)['passed']
        self.assertTrue(check(control, fault))
        self.assertFalse(check(control, fault, 2))
        for changes in ({'detected': False}, {'errors': 1}, {'exercised': False}):
            self.assertFalse(check(control, dict(fault, **changes)))
        self.assertFalse(check({'control': {'tests': 0}}, fault))
        self.assertFalse(check({'control': {'tests': 1, 'errors': 1}}, fault))
        self.assertFalse(quality.mutation_result('not json', 1)['passed'])

    def test_stale_failed_and_empty_reports_are_not_current(self):
        for report in ({'source_digest': 'old', 'status': 'passed'},
                       {'source_digest': 'new', 'status': 'failed'},
                       {'source_digest': 'new', 'status': 'passed'}):
            self.assertFalse(quality.current(report, 'new'))

    def test_quick_report_cannot_satisfy_full_gate(self):
        report = {'source_digest': 'new', 'status': 'passed', 'mode': 'quick',
                  'checks': [{'passed': True, 'exit_code': 0, 'terminal_status': 'completed'}] * 3}
        self.assertTrue(quality.current(report, 'new', 'quick'))
        self.assertFalse(quality.current(report, 'new', 'full'))

    def test_failed_test_collection_cannot_pass(self):
        from tools.checks import suite
        with tempfile.TemporaryDirectory() as temporary:
            Path(temporary, 'test_bad.py').write_text('raise RuntimeError("synthetic collection failure")\n')
            result = suite(temporary)
        self.assertFalse(result['passed'])
        self.assertEqual(result['errors'], 1)

    def test_consistency_rejects_broken_links_and_version_mismatch(self):
        root = Path(__file__).absolute().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            copy = Path(temporary)
            for name in consistency.files(root) + ['VALIDATION.md', 'PRIVACY_REVIEW.md']:
                dest = copy / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                if (root / name).exists():
                    shutil.copyfile(root / name, dest)
            
            # Assert baseline has zero broken links
            errors_before = [e for e in consistency.check(copy) if 'Broken link' in e]
            self.assertEqual(errors_before, [])
            
            before = consistency.digest(copy)
            with (copy / 'README.md').open('a') as stream:
                stream.write('\n[broken](missing-document.md)\n')
            self.assertNotEqual(consistency.digest(copy), before)
            errors_after = [e for e in consistency.check(copy) if 'Broken link' in e]
            self.assertTrue(any('missing-document.md' in e for e in errors_after), f"Missing expected broken link: {errors_after}")
            
            (copy / 'budget_tools/__init__.py').write_text('__version__ = "0.0.0"\n')
            self.assertIn('Loaded version differs from source', consistency.check(copy))

    def test_consistency_rejects_obsolete_command_and_missing_manifest_file(self):
        root = Path(__file__).absolute().parents[1]
        
        # Test 1: Documentation drift (replace valid flag with invalid)
        with tempfile.TemporaryDirectory() as temporary:
            copy = Path(temporary)
            for name in consistency.files(root):
                dest = copy / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / name, dest)
            
            readme = copy / 'README.md'
            readme_text = readme.read_text()
            readme.write_text(readme_text.replace('--include-splits', '--nonexistent-review-flag'))
            
            errors = consistency.check(copy)
            self.assertTrue(any('no longer parses' in e for e in errors), f"Expected parser error, got {errors}")
            
            # Test 2: Missing required argument
            readme.write_text(readme_text.replace('--since 2026-01-01', ''))
            errors = consistency.check(copy)
            self.assertTrue(any('no longer parses' in e for e in errors), f"Expected parser error, got {errors}")
        
        # Test 3: Missing manifest file
        with tempfile.TemporaryDirectory() as temporary:
            root_tmp = Path(temporary)
            (root_tmp / 'tools').mkdir()
            (root_tmp / 'tools/source_manifest.txt').write_text('tools/source_manifest.txt\nmissing.py\n')
            with self.assertRaisesRegex(ValueError, 'Missing'):
                consistency.digest(root_tmp)

    def test_offline_guard_propagates_to_python_child(self):
        if not getattr(sys, '_ynab_offline_guard', False):
            self.skipTest('Guard contract is exercised by tools.quality')
        code = '''
import os, socket, subprocess, sys
assert getattr(sys, '_ynab_offline_guard', False)
for action in (lambda: socket.create_connection(('127.0.0.1', 1)),
               lambda: open(os.path.expanduser('~/.ynab-private-sentinel')),
               lambda: subprocess.run(['/usr/bin/security', 'help'])):
    try:
        action()
    except PermissionError:
        continue
    raise AssertionError('Offline guard permitted forbidden access')
'''
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_offline_guard_rejects_symlink_bypass(self):
        if not getattr(sys, '_ynab_offline_guard', False):
            self.skipTest('Guard contract is exercised by tools.quality')
        
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            protected_file = temp_path / 'protected.txt'
            protected_file.write_text('secret')
            
            # Setup a fake protected HOME that is not the test temp
            fake_home = temp_path / 'fake_home'
            fake_home.mkdir()
            
            # Within the temp directory, we will create a symlink to the fake home
            alias_path = temp_path / 'alias'
            alias_path.symlink_to(fake_home, target_is_directory=True)
            
            # Write a script to test the guard logic with a modified REAL_HOME
            code = f'''
import os, sys
import tools.offline_guard.sitecustomize as guard

# Mock REAL_HOME and isolation roots
guard.REAL_HOME = "{fake_home}"
guard.TEMP = "/tmp/does_not_exist"
guard.ROOT = "/tmp/does_not_exist_either"

# 1. Direct access to fake_home should be blocked
try:
    guard.check_path_safety("{fake_home}/file.txt")
except PermissionError:
    pass
else:
    raise AssertionError("Direct access to protected home allowed")

# 2. Access through the symlink alias should also be blocked
try:
    guard.check_path_safety("{alias_path}/file.txt")
except PermissionError:
    pass
else:
    raise AssertionError("Symlink alias access to protected home allowed")

# 3. Access to a regular temp file should be allowed
guard.check_path_safety("{protected_file}")
'''
            result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_actual_guard_rejects_nested_links_and_keeps_original_home(self):
        if not getattr(sys, '_ynab_offline_guard', False):
            self.skipTest('Guard contract is exercised by tools.quality')
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            protected, scratch = base / 'protected', base / 'scratch'
            protected.mkdir(); scratch.mkdir()
            (protected / 'fictional.txt').write_text('fictional only')
            (scratch / 'bridge').symlink_to(protected, target_is_directory=True)
            (scratch / 'nested').symlink_to(scratch / 'bridge/fictional.txt')
            (scratch / 'loop').symlink_to(scratch / 'loop')
            code = '''
import os, sys, sitecustomize
from pathlib import Path
assert sitecustomize.REAL_HOME == os.environ['YNAB_TEST_HOME']
sitecustomize.REAL_HOME = sys.argv[1]
sitecustomize.TEMP = sys.argv[2]
sitecustomize.ROOT = sys.argv[1] + '/source'
try:
    os.listdir(sys.argv[1])
except PermissionError:
    pass
else:
    raise AssertionError('Protected ancestor directory enumeration allowed')
for path in (sys.argv[1] + '/fictional.txt', sys.argv[2] + '/bridge/fictional.txt',
             sys.argv[2] + '/nested', sys.argv[2] + '/loop'):
    try:
        Path(path).read_text()
    except PermissionError:
        continue
    raise AssertionError('Actual audit hook permitted protected or linked access')
'''
            env = dict(os.environ, HOME=str(scratch))
            result = subprocess.run([sys.executable, '-c', code, str(protected), str(scratch)],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_inventory_never_scans_linked_directory_and_rejects_report_suffix(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'source'; root.mkdir()
            (root / 'tools').mkdir()
            (root / 'tools/source_manifest.txt').write_text('tools/source_manifest.txt\n')
            outside = Path(temporary) / 'outside'; outside.mkdir()
            (root / 'budget_tools').symlink_to(outside, target_is_directory=True)
            scanned = []
            original = os.scandir
            def tracked(path):
                scanned.append(Path(path))
                return original(path)
            with patch('os.scandir', tracked):
                with self.assertRaisesRegex(ValueError, 'linked'):
                    consistency.files(root)
            self.assertNotIn(root / 'budget_tools', scanned)
            self.assertNotIn(outside, scanned)
            (root / 'budget_tools').unlink(); (root / 'budget_tools').mkdir()
            (root / 'budget_tools/new_REPORT.md').write_text('fictional guidance')
            with self.assertRaisesRegex(ValueError, 'Unlisted source file'):
                consistency.digest(root)

    def test_designated_reference_unknown_group_and_malformed_commands_fail(self):
        root = Path(__file__).absolute().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            copy = Path(temporary)
            for name in consistency.files(root) + ['VALIDATION.md', 'PRIVACY_REVIEW.md']:
                dest = copy / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / name, dest)
            self.assertEqual(consistency.check(copy), [])
            reference = copy / 'skills/ynab-agent-skill/references/budget-planning.md'
            original = reference.read_text()
            reference.write_text(original.replace('--assigned', '--nonexistent-review-flag'))
            self.assertTrue(any('no longer parses' in e and 'budget-planning.md' in e
                                for e in consistency.check(copy)))
            reference.write_text(original)
            readme = copy / 'README.md'; original = readme.read_text()
            readme.write_text(original.replace('transactions list', 'transactons list'))
            self.assertTrue(any('no longer parses' in e for e in consistency.check(copy)))
            readme.write_text(original + '\n```ynab\ntransactions list --since "unterminated\n```\n')
            self.assertTrue(any('Malformed documented command' in e for e in consistency.check(copy)))
            readme.write_text(original + '\n```ynab\ntransactions list --since 2001-01-01\n')
            self.assertTrue(any('Unclosed documented command' in e for e in consistency.check(copy)))

    def test_inventory_rejects_unverified_child_mount_before_stat(self):
        from tools.install_skill import InstallError, LocalPaths
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'tools').mkdir()
            (root / 'tools/source_manifest.txt').write_text('tools/source_manifest.txt\n')
            (root / 'budget_tools').mkdir()
            original = LocalPaths.check
            def local_only(safe, path):
                if Path(path) == root / 'budget_tools':
                    raise InstallError('Fictional unverified mount')
                return original(safe, path)
            from contextlib import contextmanager
            stat_calls = []
            original_scan = os.scandir
            class Entry:
                def __init__(self, entry):
                    self.entry = entry
                    self.name, self.path = entry.name, entry.path
                def stat(self, **options):
                    stat_calls.append(Path(self.path))
                    return self.entry.stat(**options)
            @contextmanager
            def tracked_scan(path):
                with original_scan(path) as entries:
                    yield (Entry(entry) for entry in entries)
            with patch.object(LocalPaths, 'check', local_only), patch('os.scandir', tracked_scan):
                with self.assertRaisesRegex(ValueError, 'unsafe'):
                    consistency.files(root)
            self.assertNotIn(root / 'budget_tools', stat_calls)

    def test_manifest_fifo_is_rejected_without_opening(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); (root / 'tools').mkdir()
            os.mkfifo(root / 'tools/source_manifest.txt')
            with self.assertRaisesRegex(ValueError, 'regular file'):
                consistency.files(root)
