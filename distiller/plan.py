"""Plan step: the model looks at the book (contents + opening) and decides how long its summary should be.

Length is in reading minutes. It is split into parts of at most `part_minutes` (the upper bound of the
"20–30 minute read" line in style.md), capped by `max_minutes` in config.json (120 = at most 4 parts).
"""
from __future__ import annotations

import math
import re

from .extract import _obj, text
from .ingest import Doc
from .llm import LMStudio

WPM = 230            # reading speed used for all minute <-> word conversions
SYNTH_MAX = 2600     # the whole-book overview (TL;DR … cheat sheet) never grows past this


def part_minutes(style: str) -> int:
    m = re.search(r"(\d+)\s*(?:[–-]|to)\s*(\d+)[\s-]*min(?:ute)?s?\b", style, re.I)
    return int(m.group(2)) if m else 30


def plan_book(llm: LMStudio, doc: Doc, chunks: list, cfg: dict, style: str) -> dict:
    lo, hi = part_minutes(style) - 10, int(cfg.get("max_minutes", 120))
    words = sum(c.tokens for c in chunks) * 3 // 4
    toc = "\n".join(f"{c.index}. {c.title} (~{c.tokens * 3 // 4:,} words)" for c in chunks)
    opening = doc.text[chunks[0].start:chunks[0].start + 6000]
    schema = _obj(read_minutes={"type": "integer", "minimum": lo, "maximum": hi}, reason=text(20))
    p = llm.chat_json([{"role": "user", "content": f"""Book: "{doc.title}"{f" by {doc.author}" if doc.author else ""}
Length: ~{words:,} words (about {words / WPM / 60:.1f} hours to read in full), {len(chunks)} sections.

CONTENTS:
{toc}

OPENING:
<<<
{opening}
>>>

Decide how long an executive summary of this book should be, in reading minutes ({lo}–{hi}).
Guide: short or light books, or books that repeat one idea: {lo}–30. Typical non-fiction: 30–60.
Long, dense books with many distinct ideas, frameworks or arguments: 60–{hi}. Never pad: give a book only
the time its ideas need. reason: one sentence on why."""}], schema, "plan", max_tokens=300)
    minutes = max(lo, min(hi, int(p["read_minutes"])))
    pm = part_minutes(style)
    return {"minutes": minutes, "parts": max(1, min(math.ceil(minutes / pm), math.ceil(hi / pm))),
            "reason": p["reason"]}


def budgets(plan: dict, chunks: list) -> tuple:
    """(words per chunk's notes, words for the whole-book overview)."""
    total = plan["minutes"] * WPM
    synth = min(SYNTH_MAX, int(total * 0.45))
    chapters = int((total - synth) * 0.85)  # the rest is room for the verified quotes
    return max(120, chapters // max(1, len(chunks))), synth


def split_parts(sizes: list, n: int, first_extra: int) -> list:
    """Split chapters (in order, by rendered words) into n contiguous parts of similar length.
    Part 1 also carries the whole-book overview (`first_extra` words). Returns a part number per chapter."""
    n = max(1, min(n, len(sizes)))
    target = (sum(sizes) + first_extra) / n
    out, part, load = [], 1, first_extra
    for i, s in enumerate(sizes):
        left = len(sizes) - i
        if part < n and load > 0 and (load + s / 2 > target or left == n - part):
            part, load = part + 1, 0
        out.append(part)
        load += s
    return out
