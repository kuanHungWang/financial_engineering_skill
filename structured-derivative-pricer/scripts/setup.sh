#!/usr/bin/env bash
# Create <skill>/.venv with QuantLib and the other requirements (idempotent). Prints the python path to use.
# A stand-alone copy (export_engine.py) has requirements.lock.txt and .python-version: its .venv holds exactly those
# packages on that Python, and is rebuilt from scratch whenever either file changes (e.g. after an engine upgrade).
set -euo pipefail
SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$SKILL_DIR/.venv"
PY="$VENV/bin/python"
LOCK="$SKILL_DIR/requirements.lock.txt"
PYVER="$(cat "$SKILL_DIR/.python-version" 2>/dev/null || echo 3.11)"

if [ -f "$LOCK" ]; then
  STAMP="$VENV/installed-requirements.txt"
  WANT="$(cat "$LOCK"; echo "python=$PYVER")"
  if [ -x "$PY" ] && [ "$WANT" = "$(cat "$STAMP" 2>/dev/null)" ]; then
    echo "$PY"; exit 0
  fi
  rm -rf "$VENV"
  if command -v uv >/dev/null 2>&1; then
    uv venv -q "$VENV" --python "$PYVER" >&2
    uv pip install -q --python "$PY" -r "$LOCK" >&2
  else
    BASE="$(command -v "python$PYVER" || command -v python3)"
    "$BASE" -m venv "$VENV" >&2
    HAVE="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    [ "$HAVE" = "$PYVER" ] || echo "warning: the prices were recorded on Python $PYVER, this .venv uses $HAVE;" \
      "install Python $PYVER (or uv) for exact reproduction" >&2
    "$PY" -m pip install -q --disable-pip-version-check -r "$LOCK" >&2
  fi
  "$PY" -c "import QuantLib" >&2
  echo "$WANT" > "$STAMP"
  echo "$PY"; exit 0
fi

# the skill itself: any versions meeting requirements.txt
if [ -x "$PY" ] && "$PY" -c "import QuantLib, pydantic, yaml" 2>/dev/null; then
  echo "$PY"; exit 0
fi
if command -v uv >/dev/null 2>&1; then
  uv venv -q "$VENV" --python "$PYVER" >&2
  uv pip install -q --python "$PY" -r "$SKILL_DIR/requirements.txt" >&2
else
  python3 -m venv "$VENV" >&2
  "$PY" -m pip install -q -r "$SKILL_DIR/requirements.txt" >&2
fi
"$PY" -c "import QuantLib" >&2
echo "$PY"
