"""The Changelog page: the releases in CHANGELOG.md (at the top of the repo,
copied into the image), newest first.

The file is plain Markdown in a fixed shape::

    ## 1.17.0 — 2026-10-08
    - What changed, in a sentence. `code` is shown as code.

Anything else in it (the title, the intro) is not shown.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from markupsafe import Markup, escape

PATH = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
_HEAD = re.compile(r"^##\s+(\S+)\s*(?:[—–-]\s*(\S.*))?$")


def _inline(text: str) -> Markup:
    parts = re.split(r"`([^`]+)`", text)
    out = [escape(p) if i % 2 == 0 else Markup("<code>") + escape(p) + Markup("</code>") for i, p in enumerate(parts)]
    return Markup("").join(out)


def parse(text: str) -> list[dict]:
    releases: list[dict] = []
    for line in text.splitlines():
        m = _HEAD.match(line.strip())
        if m:
            releases.append({"version": m.group(1), "date": (m.group(2) or "").strip(), "items": []})
        elif releases and line.startswith("- "):
            releases[-1]["items"].append(_inline(line[2:].strip()))
        elif releases and line.startswith("  ") and releases[-1]["items"]:
            releases[-1]["items"][-1] += Markup(" ") + _inline(line.strip())  # a wrapped line
    return releases


@lru_cache(maxsize=1)
def releases() -> list[dict]:
    try:
        return parse(PATH.read_text(encoding="utf-8"))
    except OSError:
        return []
