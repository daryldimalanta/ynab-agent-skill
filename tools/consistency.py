# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta
"""Bounded public-source contracts; prose still needs semantic review."""
import ast
import hashlib
from pathlib import Path
import re


def files(root):
    import os
    import stat
    from tools.install_skill import LocalPaths, InstallError

    try:
        safe = LocalPaths()
        root = safe.check(root)  # No resolve/stat/traversal before local verification.
        names = safe.read(root / 'tools/source_manifest.txt').decode().splitlines()
    except InstallError as exc:
        raise ValueError(str(exc)) from exc
    if len(names) != len(set(names)):
        raise ValueError('Duplicate source manifest entry')
    for name in names:
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or not name or name.startswith('private/'):
            raise ValueError('Unsafe source manifest entry')

    public_dirs = {'bin', 'budget_tools', 'skills', 'tests', 'tools', 'validation'}
    excluded_dirs = {'.git', '.venv', 'venv', 'tmp', 'build', 'dist', '.ruff_cache', '.mypy_cache'}
    actual_files = set()

    def scan(directory, top=False):
        # Entries are inspected lexically; no DirEntry.is_file default follows links.
        safe.check(directory)
        with os.scandir(directory) as entries:
            for entry in entries:
                name = entry.name
                try:
                    safe.check(Path(entry.path))  # Verify child mount before even lstat.
                except InstallError as exc:
                    raise ValueError('Missing or linked/unsafe source entry: ' + name) from exc
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise ValueError('Missing or linked source entry: ' + name)
                if name == '.DS_Store' or name == '__pycache__' or name.endswith('.pyc'):
                    continue
                if stat.S_ISDIR(info.st_mode):
                    if top and (name in excluded_dirs or name.endswith('.egg-info')):
                        continue
                    if top and name not in public_dirs:
                        raise ValueError('Unlisted source directory found: ' + name)
                    scan(Path(entry.path))
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    actual_files.add(Path(entry.path).relative_to(root).as_posix())
                else:
                    raise ValueError('Expected an unlinked regular source file: ' + name)

    scan(root, top=True)
    # Only these dated generated root reports are excluded to avoid circular hashes.
    actual_files -= {'VALIDATION.md', 'PRIVACY_REVIEW.md'}
    omitted = actual_files - set(names)
    if omitted:
        raise ValueError('Unlisted source file found: ' + sorted(omitted)[0])
    missing = set(names) - actual_files
    if missing:
        raise ValueError('Missing manifest file: ' + sorted(missing)[0])

    return names


def digest(root):
    value = hashlib.sha256()
    for name in sorted(files(root)):
        value.update(name.encode() + b'\0' + (root / name).read_bytes() + b'\0')
    return value.hexdigest()


def check(root):
    from budget_tools.cli import build_parser
    from budget_tools import __version__
    from tools.install_skill import SKILL_FILES
    errors = []
    try:
        names = files(root)
    except ValueError as exc:
        return [str(exc)]
    source = ast.parse((root / 'budget_tools/__init__.py').read_text())
    version = next((ast.literal_eval(n.value) for n in source.body if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == '__version__' for t in n.targets)), None)
    if version != __version__:
        errors.append('Loaded version differs from source')
    skill = root / 'skills/ynab-agent-skill'
    if (skill / 'LICENSE.txt').read_bytes() != (root / 'LICENSE').read_bytes():
        errors.append('Standalone skill license differs')
    text = (skill / 'SKILL.md').read_text()
    if not text.startswith('---\n') or '\nname: ynab-agent-skill\n' not in text:
        errors.append('Skill discovery metadata mismatch')
    if '$ynab-agent-skill' not in (skill / 'agents/openai.yaml').read_text():
        errors.append('Codex default prompt missing canonical skill invocation')
    for name in SKILL_FILES:
        if 'skills/ynab-agent-skill/' + name not in names:
            errors.append('Installer artifact absent from source manifest: ' + name)
    for name in names:
        if name.endswith('.md') and name not in {'VALIDATION.md', 'PRIVACY_REVIEW.md'}:
            for target in re.findall(r'\]\(([^)]+)\)', (root / name).read_text()):
                if '://' in target or target.startswith('#'):
                    continue
                import os
                destination = Path(os.path.abspath((root / name).parent / target.split('#')[0]))
                if not destination.is_relative_to(root) or not destination.is_file():
                    errors.append('Broken link in ' + name + ': ' + target)
    # Designated CLI blocks are parser-checked everywhere, never executed.
    import contextlib
    import io
    import shlex
    parser = build_parser()
    for doc_name in (name for name in names if name.endswith('.md')):
        text = (root / doc_name).read_text()
        blocks = re.findall(r'```ynab\n(.*?)\n```', text, re.DOTALL)
        if len(blocks) != text.count('```ynab\n'):
            errors.append('Unclosed documented command block in ' + doc_name)
        for block in blocks:
            merged = []
            current = ''
            for line in block.splitlines():
                if line.endswith('\\'):
                    current += line[:-1] + ' '
                else:
                    merged.append((current + line).strip())
                    current = ''
            if current:
                errors.append('Unfinished documented command in ' + doc_name)
            for command in merged:
                if not command or command.startswith('#'):
                    continue
                try:
                    tokens = shlex.split(command)
                except ValueError:
                    errors.append('Malformed documented command in ' + doc_name)
                    continue
                if tokens and tokens[0] == 'ynab-agent-skill':
                    tokens = tokens[1:]
                # A trailing ellipsis means optional extra flags, not an omitted
                # required argument. Parse the remaining example in full.
                if tokens and tokens[-1] == '...':
                    tokens.pop()
                try:
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        parser.parse_args(tokens)
                except SystemExit as exc:
                    if exc.code != 0:
                        errors.append('Documented command in ' + doc_name + ' no longer parses: ' + ' '.join(tokens[:2]))
    return errors
