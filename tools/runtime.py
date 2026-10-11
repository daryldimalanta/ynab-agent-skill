# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta
"""Select the checkout environment without changing system Python or user config."""

import os
from pathlib import Path
import stat
import sys

from tools.install_skill import InstallError, LocalPaths


def interpreter(root):
    safe = LocalPaths()
    root = safe.check(root)
    candidate = root / ".venv/bin/python"
    safe.check(candidate.parent)
    # A venv executable may link to a managed local runtime. Check each underlying
    # mount before lstat/readlink; never resolve or probe an unknown destination.
    pending = list(candidate.parts[1:])
    current = Path("/")
    links = 0
    while pending:
        part = pending.pop(0)
        if part in {"", "."}:
            continue
        if part == "..":
            current = current.parent
            continue
        current /= part
        matches = [(p, t) for p, t in safe.mounts if p == current or p in current.parents]
        if not matches or max(matches, key=lambda item: len(str(item[0])))[1] not in {
            "apfs",
            "hfs",
        }:
            raise InstallError("Python runtime must be on verified local storage")
        try:
            info = current.lstat()
        except FileNotFoundError:
            if sys.version_info >= (3, 10):
                return Path(sys.executable)
            raise InstallError(
                "Python 3.10+ required; create .venv with Python 3.12 (see README)"
            ) from None
        if stat.S_ISLNK(info.st_mode):
            links += 1
            if links > 40:
                raise InstallError("Python runtime link cycle")
            target = Path(os.readlink(current))
            parts = list(target.parts)
            current = current.parent
            if target.is_absolute():
                current = Path("/")
                parts = parts[1:]
            pending = parts + pending
    if not stat.S_ISREG(current.lstat().st_mode):
        raise InstallError("Python runtime executable is not a regular file")
    return candidate


def bootstrap(root, arguments):
    """Re-execute with the project venv; reject unsupported fallback interpreters."""
    try:
        target = interpreter(root)
    except InstallError as exc:
        raise SystemExit(str(exc)) from exc
    if os.path.abspath(sys.executable) != str(target):
        os.execv(str(target), [str(target), *arguments])
    if sys.version_info < (3, 10):
        raise SystemExit("Python 3.10+ required; recreate .venv with Python 3.12")
