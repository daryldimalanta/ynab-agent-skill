#!/usr/bin/env bash
set -euo pipefail
# Use the same interpreter as the application; suppress source-tree bytecode writes.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" || { echo "Python 3.10+ required"; exit 1; }
exec python3 -B "$SCRIPT_DIR/tools/install_skill.py" "$@"
