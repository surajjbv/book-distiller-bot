"""Per-chunk structured extraction and verbatim quote verification."""
from __future__ import annotations

import re

from rapidfuzz import fuzz

from .chunk import Chunk, chunk_text_for_llm
from .ingest import Doc
from .llm import LMStudio

S = {"type": "string"}
STRS = {"type": "array", "items": S}


def text(min_len: int = 1) -> dict:
    return {"type": "string", "minLength": min_len}


def arr(items: dict, lo: int = 0, hi: int | None = None) -> dict:
    """Array with item bounds. LM Studio's grammar enforces these, so the model can't return empty or bloated lists."""
    a = {"type": "array", "items": items, "minItems": lo}
    if hi is not None:
        a["maxItems"] = hi
    return a


def _obj(**props) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


def note_schema(words: int | None) -> dict:
    """Notes schema whose list sizes scale with the section's word budget (LM Studio's grammar enforces them)."""
    w = words or 600
    n = lambda per, lo, hi: max(lo, min(hi, round(w / per)))  # items affordable at ~`per` words each
    return _obj(
        core_idea=text(20),
        summary=text(150),
        key_arguments=arr(text(20), 2, n(90, 2, 5)),
        frameworks=arr(_obj(name=text(), description=text(20), steps=arr(text(), 0, 8)), 0, n(200, 1, 3)),
        data_points=arr(_obj(fact=text(5), context=text()), 0, n(200, 1, 4)),
        examples=arr(_obj(title=text(), story=text(20), lesson=text(10)), 0, n(250, 1, 2)),
        quotes=arr(_obj(text=text(20), page=S), 1, n(200, 2, 3)),
        takeaways=arr(text(15), 1, n(120, 2, 4)),
    )


NOTE_SCHEMA = note_schema(None)

SYSTEM = ("You are an expert analyst who distils non-fiction books into precise, structured notes for "
          "an executive summary. Use only what the provided text says — never add outside knowledge, "
          "never invent frameworks, numbers or quotes. Return JSON only.")


def extraction_messages(doc: Doc, c: Chunk, n_chunks: int, style: str, words: int | None) -> list:
    pages = ""
    if c.page_first:
        pages = f", pages {c.page_first}–{c.page_last}" if c.page_last != c.page_first else f", page {c.page_first}"
    budget = (f"\n\nLENGTH BUDGET: this book is summarised in {n_chunks} sections, so keep everything you write "
              f"for this section to at most {words} words in total across all fields (quotes excluded). This is a hard "
              "limit: use fewer items rather than longer ones, and keep only the most important."
              if words else "")
    user = f"""Book: "{doc.title}"{f" by {doc.author}" if doc.author else ""}
Section {c.index} of {n_chunks}: "{c.title}"{pages}

SECTION TEXT:
<<<
{chunk_text_for_llm(doc, c)}
>>>

STYLE GUIDE (apply its tone, depth and formatting; the LENGTH BUDGET below overrides its length targets):
<<<
{style}
>>>

Extract structured notes for this section:
- core_idea: the single most important idea, in one sentence.
- summary: a dense summary of the section{f" (about {int(words * 0.4)} words)" if words else ""}.
- key_arguments: the 3–5 main claims, each with its reasoning or evidence.
- frameworks: named models, step-by-step methods, matrices or rules of thumb the author proposes (name, what it is, its steps or components). Empty list if none — do not invent any.
- data_points: specific numbers, studies, statistics or research findings, each with context. Empty list if none.
- examples: up to 2 standout stories or case studies (title, 1–2 sentence story, lesson).
- quotes: 2–3 striking sentences (8–40 words each) copied EXACTLY, character for character, from the section text. "page" is the number from the nearest preceding [p. N] marker ("" if there are none). Never paraphrase, merge or shorten inside a quote and never include [p. N] markers.
- takeaways: 2–4 concrete actions a reader could apply.{budget}"""
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def extract_chunk(llm: LMStudio, doc: Doc, c: Chunk, n_chunks: int, style: str, words: int | None) -> dict:
    return llm.chat_json(extraction_messages(doc, c, n_chunks, style, words), note_schema(words), "chapter_notes")


# ====================================================================== quote verification

_TRANS = str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
                        "“": '"', "”": '"', "„": '"', "″": '"',
                        "–": "-", "—": "-", "‒": "-", "−": "-", "­": None,
                        " ": " ", "…": "...", "`": "'"})


def normalize_with_map(s: str):
    """Lowercase, unify quotes/dashes, collapse whitespace. Returns (norm, map norm-index → original index)."""
    out, idx = [], []
    prev_space = True
    for i, ch in enumerate(s):
        t = ch.translate(_TRANS) if ord(ch) > 127 or ch == "`" else ch
        if not t:
            continue
        for tc in t:
            if tc.isspace():
                if prev_space:
                    continue
                tc, prev_space = " ", True
            else:
                prev_space = False
            out.append(tc.lower())
            idx.append(i)
    return "".join(out), idx


MARKER_RE = re.compile(r"\[p\.\s*[^\]]*\]\s*")


def verify_quotes(doc: Doc, c: Chunk, note: dict, threshold: int) -> tuple:
    """Keep only quotes that exist in the chunk's source text. Fixes the quote to the exact source
    wording and replaces the page number with the true page. Returns (quotes, stats)."""
    src = doc.text[c.start:c.end]
    norm, idx = normalize_with_map(src)
    kept, stats = [], {"total": 0, "kept": 0, "exact": 0, "fuzzy": 0, "dropped": []}
    seen = set()
    for q in note.get("quotes", []):
        raw = MARKER_RE.sub("", q.get("text", "")).strip()
        stats["total"] += 1
        qn = normalize_with_map(raw)[0].strip(" \"'.,;:")
        if len(qn) < 15:
            stats["dropped"].append(raw)
            continue
        pos = norm.find(qn)
        if pos >= 0:
            a, b, score = pos, pos + len(qn), 100.0
        else:
            al = fuzz.partial_ratio_alignment(qn, norm, score_cutoff=threshold)
            if al is None or (al.dest_end - al.dest_start) < len(qn) * 0.85:
                stats["dropped"].append(raw)
                continue
            a, b, score = al.dest_start, al.dest_end, al.score
        oa, ob = idx[a], idx[b - 1] + 1
        # widen to whole words
        while oa > 0 and src[oa - 1].isalnum():
            oa -= 1
        while ob < len(src) and src[ob].isalnum():
            ob += 1
        text = re.sub(r"\s+", " ", src[oa:ob]).strip()
        if text.lower() in seen:
            continue
        seen.add(text.lower())
        stats["kept"] += 1
        stats["exact" if score >= 100 else "fuzzy"] += 1
        kept.append({"text": text, "page": doc.page_at(c.start + oa) or "", "score": round(score, 1)})
    return kept, stats
