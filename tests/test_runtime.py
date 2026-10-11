# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools import runtime
from tools.install_skill import InstallError


class RuntimeTests(unittest.TestCase):
    def test_unsupported_python_without_project_environment_fails_helpfully(self):
        with tempfile.TemporaryDirectory() as temporary:
            for version in ((3, 9, 6), (3, 10, 0), (3, 11, 0), (3, 13, 0), (4, 0, 0)):
                with self.subTest(version=version), patch.object(sys, 'version_info', version):
                    with self.assertRaisesRegex(InstallError, 'Python 3.12 required; create .venv'):
                        runtime.interpreter(Path(temporary))

    def test_supported_patch_versions_allow_path_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            for version in ((3, 12, 0), (3, 12, 15), (3, 12, 99)):
                with self.subTest(version=version), patch.object(sys, 'version_info', version):
                    self.assertEqual(runtime.interpreter(Path(temporary)), Path(sys.executable))

    def test_selected_environment_rejects_unsupported_minor_versions(self):
        with patch.object(runtime, 'interpreter', return_value=Path(sys.executable)), \
                patch.object(os, 'execv') as execute:
            for version in ((3, 11, 0), (3, 13, 0)):
                with self.subTest(version=version), patch.object(sys, 'version_info', version):
                    with self.assertRaisesRegex(SystemExit, 'Python 3.12 required; recreate .venv'):
                        runtime.bootstrap(Path('/fictional-checkout'), ['--help'])
            execute.assert_not_called()

    def test_selected_supported_environment_continues_without_reexecution(self):
        with patch.object(runtime, 'interpreter', return_value=Path(sys.executable)), \
                patch.object(sys, 'version_info', (3, 12, 15)), patch.object(os, 'execv') as execute:
            runtime.bootstrap(Path('/fictional-checkout'), ['--help'])
            execute.assert_not_called()

    def test_package_entrypoint_rejects_unsupported_python_before_dispatch(self):
        from budget_tools import cli
        with patch.object(cli, 'build_parser') as parser, patch.object(cli, 'StateStore') as state:
            for version in ((3, 9, 6), (3, 10, 0), (3, 11, 0), (3, 13, 0), (4, 0, 0)):
                with self.subTest(version=version), patch.object(sys, 'version_info', version):
                    with self.assertRaisesRegex(SystemExit, 'Python 3.12 required'):
                        cli.main(['--help'])
            parser.assert_not_called()
            state.assert_not_called()

    def test_runtime_link_to_unverified_storage_rejected_before_probe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / '.venv/bin').mkdir(parents=True)
            (root / '.venv/bin/python').symlink_to('/fictional-unverified/python')
            mounts = subprocess.CompletedProcess(['/sbin/mount'], 0,
                'fixture on / (apfs, local)\nfixture on /fictional-unverified (smbfs)\n', '')
            observed = []
            original = Path.lstat
            def tracked(path, *args, **kwargs):
                observed.append(path)
                return original(path, *args, **kwargs)
            with patch('tools.install_skill.subprocess.run', return_value=mounts), patch.object(Path, 'lstat', tracked):
                with self.assertRaisesRegex(InstallError, 'verified local'):
                    runtime.interpreter(root)
            self.assertNotIn(Path('/fictional-unverified'), observed)
            self.assertNotIn(Path('/fictional-unverified/python'), observed)
