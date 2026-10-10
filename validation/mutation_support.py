# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta

"""Inject a mutation into every workflow module that binds a helper.

Workflow helpers are defined in one module and imported by others. Patching
only one binding would leave the other callers untouched, so a fault could
survive because it was never exercised. ``patch_everywhere`` replaces every
binding of the same function and refuses ambiguous or missing targets.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from typing import Any, Iterator

from budget_tools import cli, transactions, utils

WORKFLOW_MODULES = (cli, transactions, utils)


@contextmanager
def patch_everywhere(symbol: str, replacement: Any,
                     modules: tuple[Any, ...] = WORKFLOW_MODULES) -> Iterator[Any]:
    """Replace ``symbol`` in each of ``modules`` that binds the same object."""
    targets = [module for module in modules if hasattr(module, symbol)]
    if not targets:
        raise AttributeError(f"No listed module binds {symbol!r}")
    if len({id(getattr(module, symbol)) for module in targets}) != 1:
        raise AssertionError(f"Listed modules bind different objects to {symbol!r}")
    with ExitStack() as stack:
        for module in targets:
            stack.enter_context(_swap(module, symbol, replacement))
        yield replacement


@contextmanager
def _swap(module: Any, symbol: str, replacement: Any) -> Iterator[None]:
    original = getattr(module, symbol)
    setattr(module, symbol, replacement)
    try:
        yield
    finally:
        setattr(module, symbol, original)
