# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta
"""Verify exact public wheel members and their source bytes before distribution."""

import argparse
from email.parser import BytesParser
from pathlib import Path
from zipfile import ZipFile

from budget_tools import __version__
from tools.consistency import files
from tools.install_skill import LocalPaths


def verify(root, archive):
    names = files(root)
    package = {name for name in names if name.startswith("budget_tools/") and name.endswith(".py")}
    metadata_dir = "ynab_agent_skill-" + __version__ + ".dist-info/"
    extra = {"METADATA", "WHEEL", "entry_points.txt", "top_level.txt", "RECORD", "licenses/LICENSE"}
    expected = package | {metadata_dir + name for name in extra}
    safe = LocalPaths()
    # Verify the local path before ZipFile opens it. No remote artifacts or extraction.
    path = safe.check(archive)
    with ZipFile(path) as wheel:
        members = wheel.namelist()
        if len(members) != len(set(members)) or set(members) != expected:
            raise ValueError("Wheel contains missing, duplicate or unapproved files")
        for name in package:
            if wheel.read(name) != safe.read(root / name):
                raise ValueError("Wheel source bytes differ: " + name)
        if wheel.read(metadata_dir + "licenses/LICENSE") != safe.read(root / "LICENSE"):
            raise ValueError("Wheel license differs")
        raw_metadata = wheel.read(metadata_dir + "METADATA")
        metadata = BytesParser().parsebytes(raw_metadata)
        if raw_metadata.partition(b"\n\n")[2] != safe.read(root / "README.md"):
            raise ValueError("Wheel README metadata differs")
        if metadata["Name"] != "ynab-agent-skill" or metadata["Version"] != __version__:
            raise ValueError("Wheel package identity differs")
        if metadata["Requires-Python"] != "<3.13,>=3.12" or metadata.get_all("Requires-Dist"):
            raise ValueError("Wheel runtime requirements differ")
        entrypoints = wheel.read(metadata_dir + "entry_points.txt").decode()
        for name in ("ynab-agent-skill", "ynab-codex-skill"):
            if name + " = budget_tools.cli:main" not in entrypoints:
                raise ValueError("Wheel command entry point differs")
    return len(members)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    count = verify(Path(__file__).absolute().parents[1], args.archive)
    print("Verified public wheel: " + str(count) + " allowlisted files")


if __name__ == "__main__":
    main()
