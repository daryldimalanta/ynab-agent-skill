# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta
"""Bounded public-source contracts; prose still needs semantic review."""
import ast
import hashlib
from pathlib import Path
import re


def files(root):
    root = root.resolve()
    names_text = (root / 'tools/source_manifest.txt').read_text()
    names = names_text.splitlines()
    if len(names) != len(set(names)):
        raise ValueError('Duplicate source manifest entry')
    
    # Establish actual files in public locations
    actual_files = set()
    for directory in ['.', 'bin', 'budget_tools', 'skills', 'tests', 'tools', 'validation']:
        dir_path = root / directory
        if not dir_path.is_dir():
            continue
        if directory == '.':
            for p in dir_path.iterdir():
                if p.is_file() and p.name not in {'.DS_Store'} and not p.name.endswith('.pyc'):
                    actual_files.add(p.relative_to(root).as_posix())
        else:
            for p in dir_path.rglob('*'):
                if p.is_file():
                    if '.DS_Store' in p.parts or '__pycache__' in p.parts or p.name.endswith('.pyc'):
                        continue
                    actual_files.add(p.relative_to(root).as_posix())
    
    # Exclude generated reports if they are somehow placed in root
    actual_files = {f for f in actual_files if not f.endswith('_REPORT.md') and f not in {'VALIDATION.md', 'PRIVACY_REVIEW.md'}}
    
    manifest_set = set(names)
    omitted = actual_files - manifest_set
    if omitted:
        raise ValueError('Unlisted source file found: ' + sorted(omitted)[0])

    for name in names:
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or not name or name.startswith('private/'):
            raise ValueError('Unsafe source manifest entry')
        target = root / path
        # Check parents up to root to avoid resolving outside bounds, but resolve tempdir links safely
        if not target.is_file():
            raise ValueError('Missing or linked manifest file: ' + name)
        
        # Verify no links inside the repository
        current = target
        while current != root:
            if current.is_symlink():
                raise ValueError('Missing or linked manifest file: ' + name)
            current = current.parent

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
                if not (root / name).parent.joinpath(target.split('#')[0]).is_file():
                    errors.append('Broken link in ' + name + ': ' + target)
    # Extract executable examples from documentation
    import shlex
    parser = build_parser()
    for doc_name in ['README.md', 'skills/ynab-agent-skill/SKILL.md']:
        text = (root / doc_name).read_text()
        for block in re.findall(r'```(?:sh|text)\n(.*?)\n```', text, re.DOTALL):
            merged = []
            current = ''
            for line in block.split('\n'):
                if line.endswith('\\'):
                    current += line[:-1] + ' '
                else:
                    current += line
                    merged.append(current.strip())
                    current = ''
            
            for cmd in merged:
                if not cmd or cmd.startswith('#') or cmd.startswith('PYTHONDONTWRITEBYTECODE') or cmd.startswith('python3'):
                    continue
                try:
                    tokens = shlex.split(cmd)
                except ValueError:
                    continue
                if not tokens:
                    continue
                if tokens[0] == 'ynab-agent-skill':
                    tokens = tokens[1:]
                if not tokens or tokens[-1] == '...':
                    continue
                if tokens[0] in {'budgets', 'transactions', 'drafts', '--version'}:
                    try:
                        import contextlib, io
                        with contextlib.redirect_stdout(io.StringIO()):
                            parser.parse_args(tokens)
                    except SystemExit as e:
                        if e.code != 0:
                            errors.append(f'Documented command in {doc_name} no longer parses: ' + ' '.join(tokens[:2]))
    return errors
