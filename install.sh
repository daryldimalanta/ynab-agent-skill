#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 -B "$SCRIPT_DIR/tools/run.py" "$@"
