# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta
"""Install the same reviewed instructions for either host; never contact YNAB."""
from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import uuid

SKILL_FILES = (
    'SKILL.md', 'LICENSE.txt', 'agents/openai.yaml',
    'references/budget-planning.md', 'references/long-runs.md',
    'references/memos.md', 'references/proposal.md', 'references/purchases.md',
)
HOSTS = {'codex': '.codex/skills', 'antigravity': '.gemini/config/skills'}
NAME = 'ynab-agent-skill'
PREFERENCE = 'references/local-preferences.md'


class InstallError(RuntimeError):
    pass


class LocalPaths:
    """Reject unknown mounts before filesystem access; reject linked ancestors.

    Cached mount metadata is read without probing or resolving other mounts.
    This protects ordinary local installs, not hostile same-user path races.
    """
    def __init__(self):
        result = subprocess.run(['/sbin/mount'], capture_output=True, text=True, check=True)
        self.mounts = []
        for line in result.stdout.splitlines():
            match = re.match(r'^.+ on (.+) \(([^,)]+)', line)
            if match:
                self.mounts.append((Path(match[1].replace('\\040', ' ')), match[2]))

    def check(self, path):
        path = Path(os.path.abspath(path))
        matches = [(p, t) for p, t in self.mounts if p == path or p in path.parents]
        if not matches or max(matches, key=lambda item: len(str(item[0])))[1] not in {'apfs', 'hfs'}:
            raise InstallError('Unsafe to proceed: destination is not verified local storage')
        for part in reversed((path,) + tuple(path.parents)):
            try:
                info = part.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode):
                raise InstallError('Unsafe to proceed: linked path component')
        return path

    def read(self, path):
        path = self.check(path)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise InstallError('Unsafe to proceed: expected an unlinked regular file')
        # Reject special files before opening. Hostile same-user replacement
        # races are outside this local installer's boundary.
        with path.open('rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise InstallError('Unsafe to proceed: expected an unlinked regular file')
            return stream.read()



def _write(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(data)
    path.chmod(0o600)


def _verify(directory, expected, safe):
    for name, data in expected.items():
        if safe.read(directory / name) != data:
            raise InstallError('Installed file verification failed')


def install(root, home, target, dry_run=False):
    safe = LocalPaths()
    root = safe.check(root)
    home = safe.check(home)
    source = root / 'skills' / NAME
    public = {name: safe.read(source / name) for name in SKILL_FILES}
    safe.check(root / 'bin' / NAME)
    if not (root / 'bin' / NAME).is_file():
        raise InstallError('Checkout executable is missing')
    hosts = tuple(HOSTS) if target == 'both' else (target,)
    backup_root = safe.check(home / '.ynab-agent-skill-backups')
    plans = []
    for host in hosts:
        parent = safe.check(home / HOSTS[host])
        dest, legacy = parent / NAME, parent / 'ynab-codex-skill'
        preferences = None
        for old in (dest, legacy):
            safe.check(old)
            if old.exists() and not old.is_dir():
                raise InstallError('Unsafe to proceed: skill destination is not a directory')
            pref = safe.check(old / PREFERENCE)
            if pref.exists():
                contents = safe.read(pref)
                if preferences is None:  # Current canonical preferences take priority.
                    preferences = contents
        if preferences is None:
            preferences = ('Use this executable: `' + str(root / 'bin' / NAME) + '`\n').encode()
        plans.append((host, parent, dest, legacy, dict(public, **{PREFERENCE: preferences})))
    if dry_run:
        for host in hosts:
            print('[DRY RUN] Would verify and install reviewed instructions for ' + host)
        return  # No temporary directories, backups, locks, or permission changes.

    backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if backup_root.stat().st_uid != os.getuid():
        raise InstallError('Backup directory must be owned by this user')
    backup_root.chmod(0o700)
    lock_path = safe.check(backup_root / '.install.lock')
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise InstallError('Unsafe installer lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Recheck preferences under the installer lock before replacing anything.
        for _, _, dest, legacy, expected in plans:
            for old in (dest, legacy):
                safe.check(old)
            selected = next((old / PREFERENCE for old in (dest, legacy)
                             if safe.check(old / PREFERENCE).exists()), None)
            if selected and safe.read(selected) != expected[PREFERENCE]:
                raise InstallError('Preferences changed during preflight; retry installation')
        run = backup_root / uuid.uuid4().hex
        run.mkdir(mode=0o700)
        moved, installed, stages = [], [], []
        try:
            for host, parent, dest, legacy, expected in plans:
                safe.check(parent).mkdir(mode=0o700, parents=True, exist_ok=True)
                stage = parent / ('.ynab-install-' + uuid.uuid4().hex)
                stage.mkdir(mode=0o700)
                stages.append(stage)
                for name, data in expected.items():
                    _write(stage / name, data)
                _verify(stage, expected, safe)
                archive = run / host
                archive.mkdir(mode=0o700)
                for old in (dest, legacy):
                    safe.check(old)
                    if old.exists():
                        saved = archive / old.name
                        old.rename(saved)
                        moved.append((saved, old))
                stage.rename(dest)
                installed.append(dest)
                _verify(dest, expected, safe)
        except BaseException:
            # Roll back both hosts on a caught failure; preserve archives if recovery fails.
            for dest in reversed(installed):
                shutil.rmtree(dest)
            for saved, old in reversed(moved):
                saved.rename(old)
            raise
        finally:
            for stage in stages:
                if stage.exists():
                    shutil.rmtree(stage)
        for host in hosts:
            print('Verified installation: ' + host)
        print('Recovery backups: ' + str(run))
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target', choices=('codex', 'antigravity', 'both'))
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        install(Path(__file__).absolute().parents[1], Path.home(), args.target, args.dry_run)
    except (OSError, InstallError, subprocess.SubprocessError) as exc:
        print('Installation failed: ' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
