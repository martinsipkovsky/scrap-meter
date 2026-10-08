"""The Tutorial: the short wiki pages in docs/wiki (Markdown with pictures)
shown inside the app, so they also work on a server without internet. The
same files read on GitHub. docs/wiki/README.md is the contents: its links,
in order, are the pages of the side list.

Links between pages (``dashboard.md``), to pictures (``images/x.png``) and
to the longer guides in docs (``../user-guide.md#dashboard``) are turned into
the app's own addresses. The files are part of the repository, not user
input, so their HTML is shown as written.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import markdown
from markupsafe import Markup

ROOT = Path(__file__).resolve().parent.parent / "docs"
WIKI = ROOT / "wiki"
IMAGES = WIKI / "images"
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_LINK = re.compile(r"\[([^\]]+)\]\(([a-z0-9-]+)\.md\)")


def _url(target: str) -> str:
    """A link in a page, as the app serves it."""
    if re.match(r"^[a-z]+:", target) or target.startswith("#") or target.startswith("/"):
        return target
    path, _, anchor = target.partition("#")
    anchor = f"#{anchor}" if anchor else ""
    if path.startswith("images/"):
        return f"/tutorial/{path}"
    if path.startswith("../") and path.endswith(".md"):
        return f"/tutorial/docs/{path[3:-3]}{anchor}"
    if path.endswith(".md"):
        name = path[:-3]
        return ("/tutorial" if name == "README" else f"/tutorial/{name}") + anchor
    return target


def _rewrite(html: str) -> str:
    return re.sub(r'(href|src)="([^"]+)"', lambda m: f'{m.group(1)}="{_url(m.group(2))}"', html)


@lru_cache(maxsize=None)
def contents() -> list[dict]:
    """The pages in the order of README.md: [{"name", "title"}]."""
    try:
        text = (WIKI / "README.md").read_text(encoding="utf-8")
    except OSError:
        return []
    return [{"name": n, "title": t} for t, n in _LINK.findall(text)]


def _title(text: str, fallback: str) -> str:
    m = re.search(r"^#\s+(.+)$", text, re.M)
    return m.group(1).strip() if m else fallback


@lru_cache(maxsize=None)
def render(folder: str, name: str) -> dict | None:
    """{"title", "html"} of a wiki page ("wiki") or a guide in docs ("docs"); None if there is none."""
    if not _NAME.match(name) and name != "README":
        return None
    base = WIKI if folder == "wiki" else ROOT
    path = base / f"{name}.md"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    html = markdown.markdown(text, extensions=["tables", "fenced_code", "toc", "sane_lists"])
    if folder == "docs":  # links inside a guide point at its neighbours in docs
        html = re.sub(r'href="(?!https?:|#|/)([a-z0-9-]+)\.md(#[^"]*)?"',
                      lambda m: f'href="/tutorial/docs/{m.group(1)}{m.group(2) or ""}"', html)
        html = re.sub(r'href="wiki/README\.md"', 'href="/tutorial"', html)
        html = re.sub(r'href="wiki/([a-z0-9-]+)\.md"', r'href="/tutorial/\1"', html)
    else:
        html = _rewrite(html)
    return {"title": _title(text, name), "html": Markup(html)}


def image(name: str) -> Path | None:
    """A picture of the wiki (only files directly in docs/wiki/images)."""
    if not re.match(r"^[a-z0-9][a-z0-9-]*\.(png|jpg|jpeg|gif|svg|webp)$", name):
        return None
    path = IMAGES / name
    return path if path.is_file() else None
