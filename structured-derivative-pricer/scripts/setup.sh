#!/usr/bin/env bash
# Create <skill>/.venv with QuantLib + fastFE (idempotent). Prints the python path to use.
set -euo pipefail
SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="$SKILL_DIR/.venv/bin/python"
if [ -x "$PY" ] && "$PY" -c "import QuantLib, fastFE, pydantic, yaml" 2>/dev/null; then
  echo "$PY"; exit 0
fi
if command -v uv >/dev/null 2>&1; then
  uv venv -q "$SKILL_DIR/.venv" --python 3.11 >&2
  uv pip install -q --python "$PY" -r "$SKILL_DIR/requirements.txt" >&2
else
  python3 -m venv "$SKILL_DIR/.venv" >&2
  "$PY" -m pip install -q -r "$SKILL_DIR/requirements.txt" >&2
fi
"$PY" -c "import QuantLib, fastFE" >&2
echo "$PY"
