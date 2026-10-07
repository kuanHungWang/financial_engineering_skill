#!/usr/bin/env python
"""Refresh the component reference in docs/index.html from the pricer's live catalog.

    PY=$(structured-derivative-pricer/scripts/setup.sh)
    $PY docs/build_catalog.py

The catalog is the same JSON `price.py catalog` prints. It is embedded between the
CATALOG:BEGIN / CATALOG:END markers, so the page stays a single static file that opens offline.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DOCS = Path(__file__).resolve().parent
sys.path.insert(0, str(DOCS.parent / "structured-derivative-pricer"))

from pricer import catalog  # noqa: E402

PAGE = DOCS / "index.html"
BLOCK = re.compile(r"(<!-- CATALOG:BEGIN -->).*?(<!-- CATALOG:END -->)", re.S)


def main() -> int:
    data = json.dumps(catalog(None), separators=(",", ":"), default=str).replace("</", "<\\/")
    tag = f'\n<script id="catalog-data" type="application/json">{data}</script>\n'
    html = PAGE.read_text()
    if not BLOCK.search(html):
        print("CATALOG markers not found in docs/index.html", file=sys.stderr)
        return 1
    PAGE.write_text(BLOCK.sub(lambda m: m.group(1) + tag + m.group(2), html))
    print(f"wrote {len(data):,} bytes of catalog into {PAGE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
