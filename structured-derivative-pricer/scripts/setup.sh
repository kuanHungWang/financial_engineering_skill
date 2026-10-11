#!/usr/bin/env bash
# Create <skill>/.venv with QuantLib and the other requirements (idempotent). Prints the python path to use.
# A stand-alone copy (export_engine.py) has requirements.lock.txt and .python-version: exactly those versions are
# installed, and installed again whenever the lock file changes (e.g. after an engine upgrade).
set -euo pipefail
SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="$SKILL_DIR/.venv/bin/python"
REQ="$SKILL_DIR/requirements.lock.txt"
[ -f "$REQ" ] || REQ="$SKILL_DIR/requirements.txt"
STAMP="$SKILL_DIR/.venv/installed-requirements.txt"
PYVER="$(cat "$SKILL_DIR/.python-version" 2>/dev/null || echo 3.11)"
if [ -x "$PY" ] && "$PY" -c "import QuantLib, pydantic, yaml" 2>/dev/null \
   && { [ "$REQ" = "$SKILL_DIR/requirements.txt" ] || cmp -s "$REQ" "$STAMP"; }; then
  echo "$PY"; exit 0
fi
if command -v uv >/dev/null 2>&1; then
  [ -x "$PY" ] || uv venv -q "$SKILL_DIR/.venv" --python "$PYVER" >&2
  uv pip install -q --python "$PY" -r "$REQ" >&2
else
  [ -x "$PY" ] || python3 -m venv "$SKILL_DIR/.venv" >&2
  "$PY" -m pip install -q -r "$REQ" >&2
fi
"$PY" -c "import QuantLib" >&2
cp "$REQ" "$STAMP"
echo "$PY"
