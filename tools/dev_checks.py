# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta
"""Repeatable correctness lint, scoped formatting, and guarded-boundary typing."""

from pathlib import Path
import subprocess
import sys

from tools.runtime import bootstrap

ROOT = Path(__file__).absolute().parents[1]
FORMATTED = (
    "budget_tools/transaction_workflow.py",
    "tools/runtime.py",
    "tools/run.py",
    "tools/check_wheel.py",
    "tools/dev_checks.py",
    "tests/test_transaction_workflow.py",
    "tests/test_packaging.py",
)


def main():
    commands = (
        ["ruff", "check", "budget_tools", "tools", "tests", "validation"],
        ["ruff", "format", "--check", *FORMATTED],
        ["mypy", "--config-file", "pyproject.toml"],
    )
    for command in commands:
        result = subprocess.run([sys.executable, "-B", "-m", *command], cwd=ROOT)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    bootstrap(ROOT, ["-B", "-m", "tools.dev_checks", *sys.argv[1:]])
    raise SystemExit(main())
