# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta
"""Accidental-access guard for reviewed offline tests, including Python children.

This is not an OS sandbox against hostile code or native system calls.
"""
import os
from pathlib import Path
import subprocess
import sys

ROOT = os.environ.get('YNAB_TEST_SOURCE')
TEMP = os.environ.get('YNAB_TEST_TEMP')
if ROOT and TEMP:
    REAL_HOME = os.environ.setdefault('YNAB_TEST_HOME', os.path.expanduser('~'))
    PYTHONPATH = os.environ.get('PYTHONPATH')
    EXECUTABLE = os.path.realpath(sys.executable)
    sys.dont_write_bytecode = True

    def within(path, root):
        return path == root or path.startswith(root + os.sep)

    def _enforce_rules(p):
        if '.keychain' in p or within(p, '/Volumes'):
            raise PermissionError('Offline checks forbid credentials and mounted storage')
        allowed_descendant = within(p, ROOT) or within(p, TEMP) or within(p, sys.base_prefix)
        allowed_ancestor = within(ROOT, p) or within(TEMP, p) or within(sys.base_prefix, p)
        if within(p, REAL_HOME) and not (allowed_descendant or allowed_ancestor):
            raise PermissionError('Offline checks forbid existing private files')
        if '/Library/Application Support/' in p and not within(p, TEMP):
            raise PermissionError('Offline checks forbid private application state')

    def check_path_safety(path_str):
        path_str = os.path.abspath(os.fsdecode(path_str))
        current = "/"
        for part in path_str.strip("/").split("/"):
            if not part:
                continue
            current = os.path.join(current, part)
            _enforce_rules(current)
            # Reject before following: even a wholly local link can contain a
            # second linked ancestor. Only fixed macOS system aliases are used.
            if os.path.islink(current):
                target = os.readlink(current)
                aliases = {"/tmp": "/private/tmp", "/var": "/private/var"}
                if aliases.get(current) != target:
                    raise PermissionError('Offline checks forbid linked path components')
                _enforce_rules(target)
                current = target

    def guard(event, args):
        if event.startswith('socket.'):
            raise PermissionError('Offline checks forbid network access')
        if event in {'open', 'os.listdir', 'os.scandir'} and args:
            path = args[0]
            if isinstance(path, (str, bytes, os.PathLike)):
                check_path_safety(path)
                absolute = os.path.abspath(os.fsdecode(path))
                if within(absolute, REAL_HOME):
                    if not any(within(absolute, root) for root in (ROOT, TEMP, sys.base_prefix)):
                        raise PermissionError('Offline checks forbid private file or directory access')
        if event == 'os.exec':
            executable, command, env = args
            child = os.environ if env is None else env
            if (os.fsdecode(executable) == ROOT + '/.venv/bin/python'
                    and list(command[1:3]) == ['-B', ROOT + '/tools/run.py']
                    and all(child.get(k) == os.environ.get(k) for k in
                            ('PYTHONPATH', 'YNAB_TEST_SOURCE', 'YNAB_TEST_TEMP', 'YNAB_TEST_HOME'))):
                return  # Reviewed installer bootstrap preserves the audit guard.
        if event in {'os.system', 'os.posix_spawn', 'os.exec', 'os.fork'}:
            raise PermissionError('Unexpected offline process launch')
        if event == 'subprocess.Popen':
            executable, command, cwd, env = args
            executable = os.fsdecode(executable)
            child = os.environ if env is None else env
            if any(child.get(k) != os.environ.get(k) for k in ('PYTHONPATH', 'YNAB_TEST_SOURCE', 'YNAB_TEST_TEMP', 'YNAB_TEST_HOME')):
                raise PermissionError('Child process dropped the offline guard')
            if executable == ROOT + '/install.sh' and within(child.get('HOME', ''), TEMP):
                return
            if (os.path.basename(executable) in ('python3', 'python') or os.path.realpath(executable) == EXECUTABLE) and len(command) >= 3 and command[1] == '-c':
                return
            raise PermissionError('Unexpected subprocess in offline checks')

    original_run = subprocess.run
    def local_run(command, *args, **kwargs):
        if command == ['/sbin/mount']:
            # Synthetic local metadata; never enumerate actual network mounts in tests.
            return subprocess.CompletedProcess(command, 0, 'fixture on / (apfs, local)\n', '')
        return original_run(command, *args, **kwargs)
    subprocess.run = local_run
    sys.addaudithook(guard)
    sys._ynab_offline_guard = True
