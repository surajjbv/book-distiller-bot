"""Render the summary with Jinja2. The LLM never writes HTML."""
from __future__ import annotations

import base64
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

from .util import TEMPLATE_DIR


def inline_md(s) -> Markup:
    """Escape, then allow **bold** / *italic* only."""
    t = str(escape(s or ""))
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"<em>\1</em>", t)
    return Markup(t)


def strip_md(s) -> str:
    return re.sub(r"\*{1,2}(.+?)\*{1,2}", r"\1", s or "")


def chapter_links(refs, hrefs: dict) -> Markup:
    """Turn "Ch 3", "Ch 3–5", "Chapters 2, 4" into links; `hrefs` maps chapter -> page file ("" = this page)."""
    if isinstance(refs, str):
        refs = [refs]
    out = []
    for r in refs or []:
        nums = [int(n) for n in re.findall(r"\d+", str(r))]
        m = re.search(r"(\d+)\s*[–-]\s*(\d+)", str(r))
        if m:
            nums = list(range(int(m.group(1)), int(m.group(2)) + 1))
        links = [f'<a class="ref" href="{hrefs[n]}#ch-{n}">Ch {n}</a>' for n in nums if n in hrefs]
        out.append(" ".join(links) if links else f'<span class="ref">{escape(r)}</span>')
    return Markup(" ".join(out))


def env() -> Environment:
    e = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=select_autoescape(["html", "j2"]),
                    trim_blocks=True, lstrip_blocks=True)
    e.filters["md"] = inline_md
    e.filters["plain"] = strip_md
    return e


def data_uri(path: Path, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def word_count(ctx: dict) -> int:
    n = 0

    def walk(v):
        nonlocal n
        if isinstance(v, str):
            if not v.startswith("data:"):
                n += len(v.split())
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk({k: ctx[k] for k in ("tldr", "core_thesis", "themes", "chapters", "frameworks", "key_stories",
                              "figures", "takeaways", "quotes", "critique", "cheat_sheet") if k in ctx})
    return n


def render_summary(ctx: dict) -> str:
    e = env()
    e.globals["chapter_links"] = lambda refs: chapter_links(refs, ctx["chapter_href"])
    return e.get_template("summary.html.j2").render(**ctx)
