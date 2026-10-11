# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta
import tempfile
from pathlib import Path
import unittest
from zipfile import ZipFile

from tools.check_wheel import verify


class PackagingTests(unittest.TestCase):
    def test_rejects_unapproved_wheel_members_before_source_readback(self):
        root = Path(__file__).absolute().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "fictional.whl"
            with ZipFile(archive, "w") as wheel:
                wheel.writestr("private/preferences.txt", "fictional only")
            with self.assertRaisesRegex(ValueError, "unapproved"):
                verify(root, archive)
