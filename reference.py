"""Reference material for the AI: a guide to designing for FDM printing and the sizes and mounting
holes of common boards and parts (reference/). Served as MCP resources and through the lookup_part
and print_guide tools, since not every client reads resources."""
from __future__ import annotations

import functools
import json
import re
from pathlib import Path

DIR = Path(__file__).parent / "reference"


@functools.lru_cache(maxsize=1)
def guide() -> str:
    return (DIR / "print-guide.md").read_text(encoding="utf-8")


@functools.lru_cache(maxsize=1)
def _data() -> dict:
    return json.loads((DIR / "parts.json").read_text(encoding="utf-8"))


def note() -> str:
    return _data()["_note"]


def parts() -> dict:
    return _data()["parts"]


def _words(s: str) -> set:
    return set(re.findall(r"[a-z0-9]+", s.lower()))


def lookup(query: str = "") -> list[dict]:
    """Parts matching every word of query (in key, name or aliases), best first; all without a query."""
    q = _words(query)
    if not q:
        return [{"key": key, **p} for key, p in parts().items()]
    hits = []
    for key, p in parts().items():
        names = [key, p["name"], *p.get("aliases", [])]
        text = _words(" ".join(names))
        if q <= text or any(query.strip().lower() == n.lower() for n in names):
            exact = any(_words(n) == q for n in names)
            hits.append((not exact, len(text), {"key": key, **p}))
    return [h[2] for h in sorted(hits, key=lambda h: h[:2])]
