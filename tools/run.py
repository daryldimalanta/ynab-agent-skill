# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta
"""Installer bootstrap; runtime choice matches the application and quality runner."""

import sys
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT))
from tools.runtime import bootstrap

bootstrap(ROOT, ["-B", str(Path(__file__).absolute()), *sys.argv[1:]])
from tools.install_skill import main

raise SystemExit(main())
