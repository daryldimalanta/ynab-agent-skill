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
            with patch.object(sys, 'version_info', (3, 9, 6)):
                with self.assertRaisesRegex(InstallError, 'create .venv'):
                    runtime.interpreter(Path(temporary))

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
