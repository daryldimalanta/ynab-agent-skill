# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta

from __future__ import annotations

import subprocess


# Compatibility labels, not credentials. A command rename must not silently
# select a new Keychain item or require exporting the existing token.
KEYCHAIN_ACCOUNT = "personal"
KEYCHAIN_SERVICE = "budget-tools-for-ynab"


class KeychainError(RuntimeError):
    """Raised when the configured macOS Keychain item cannot be read."""


def read_access_token() -> str:
    """Read the API token without writing it to stdout, logs, or a file."""
    try:
        result = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-a",
                KEYCHAIN_ACCOUNT,
                "-s",
                KEYCHAIN_SERVICE,
                "-w",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise KeychainError("macOS Keychain could not be accessed") from exc

    token = result.stdout.strip()
    if result.returncode != 0 or not token:
        raise KeychainError(
            "The Keychain item 'budget-tools-for-ynab' could not be read. "
            "Store it under account 'personal' and try again."
        )
    return token
