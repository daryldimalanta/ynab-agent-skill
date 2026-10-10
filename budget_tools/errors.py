# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta

"""Exceptions shared by command modules."""


class CliError(RuntimeError):
    """Raised for argument or state errors before a request is sent."""
