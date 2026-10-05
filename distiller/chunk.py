"""Turn a Doc into chapter-sized chunks (TOC → headings → fixed size), skipping front/back matter."""
from __future__ import annotations

import math
import re
import statistics
from dataclasses import asdict, dataclass, field

from .ingest import Doc
from .util import est_tokens, log

# Whole-title matches only: these words are too common to match as prefixes ("Notes on Power").
SKIP_EXACT = re.compile(
    r"^\s*(notes|endnotes|end notes|index|general index|references|sources|bibliography|contents|"
    r"table of contents|glossary|illustrations|figures|tables|dedication|epigraph|cover|title page|"
    r"half title|copyright|copyright page|imprint|colophon|credits|permissions|footnotes|"
    r"selected bibliography|notes and sources|works cited|about this book|also available)\s*\.?\s*$",
    re.I)
SKIP_PREFIX = re.compile(
    r"^\s*(acknowledge?ments?|about the authors?|about the publisher|also by|other books by|books by|"
    r"praise for|list of (figures|illustrations|tables|abbreviations)|further reading|copyright|"
    r"the full project gutenberg|project gutenberg|start of the project|end of the project|"
    r"newsletter|sign up|discover more|reading group guide|a note on sources|photo credits|"
    r"image credits|by the same author|contents)\b",
    re.I)
SKIP_ANY = re.compile(r"project gutenberg|gutenberg (e-?text|ebook|license)", re.I)
NUMBERED = re.compile(r"^\s*((chapter|ch\.?|lesson|step)\s*[\dIVXLC]+\b|\d{1,3}\s*[.:)\-–—]\s+\S)", re.I)
INDEX_LINE = re.compile(r"^.{2,100}?,?\s\d+(\s*[-–,]\s*\d+)*\.?$")
FRONT = re.compile(r"^\s*(preface|foreword|introduction|intro\b|prologue|prelude|author'?s note|"
                   r"a note to (the )?reader|how to (use|read) this book|overture)", re.I)
BACK = re.compile(r"^\s*(epilogue|afterword|conclusion|appendix|postscript|coda|final thoughts|"
                  r"closing thoughts|summary)", re.I)


@dataclass
class Chunk:
    index: int
    title: str
    kind: str          # front | chapter | back
    start: int
    end: int
    tokens: int
    page_first: str | None = None
    page_last: str | None = None
    titles: list = field(default_factory=list)


def classify(title: str) -> str:
    t = re.sub(r"^\s*(chapter|part)?\s*[\dIVXLC]+[.:)\-–—]?\s+", "", title or "", flags=re.I)
    if SKIP_ANY.search(title or ""):
        return "skip"
    for cand in (title or "", t):
        if SKIP_EXACT.match(cand) or SKIP_PREFIX.match(cand):
            return "skip"
    for cand in (title or "", t):
        if FRONT.match(cand):
            return "front"
        if BACK.match(cand):
            return "back"
    return "chapter"


def looks_like_index_or_notes(text: str) -> bool:
    """True when most of the text (by characters, not lines) is index entries or numbered notes."""
    lines = [l.strip() for l in re.split(r"\n+", text) if l.strip()]
    if len(lines) < 15:
        return False
    total = sum(len(l) for l in lines)
    index_chars = sum(len(l) for l in lines if INDEX_LINE.match(l))
    note_chars = sum(len(l) for l in lines if re.match(r"^\d{1,3}[.)]?\s", l))
    return index_chars / total > 0.5 or (note_chars / total > 0.6 and "ibid" in text.lower())


def trim_index_tail(text: str, a: int, b: int) -> int:
    """Return a new end offset that drops a trailing run of index-style lines (e.g. an untagged index)."""
    paras = list(re.finditer(r"[^\n]+", text[a:b]))
    cut, run = None, 0
    for m in reversed(paras):
        line = m.group().strip()
        if INDEX_LINE.match(line) or SKIP_EXACT.match(line) or SKIP_PREFIX.match(line):
            run += 1
            cut = a + m.start()
        elif len(line) < 60 and run:  # short line inside the run (letter headers like "A")
            continue
        else:
            break
    return cut if cut is not None and run >= 8 else b


def _pick_level(doc: Doc, hs: list, chunk_tokens: int) -> int:
    levels = sorted({h.level for h in hs})
    chosen = levels[0]
    for lv in levels:
        sel = [h for h in hs if h.level <= lv]
        sizes = [est_tokens(doc.text[a.offset:b.offset]) for a, b in zip(sel, sel[1:] + [None])
                 if b is not None] + [est_tokens(doc.text[sel[-1].offset:doc.content_end])]
        real = [s for h, s in zip(sel, sizes) if classify(h.title) != "skip" and s >= 300]
        chosen = lv
        if len(real) >= 4 and statistics.median(real) <= 2 * chunk_tokens:
            break
    return chosen


def build_chunks(doc: Doc, cfg: dict) -> list:
    chunk_tokens = int(cfg["chunk_tokens"])
    # Leave room for prompt + output inside the context window.
    chunk_tokens = min(chunk_tokens, int(cfg["context_length"]) - int(cfg["max_output_tokens"]) - 2500)
    min_tokens = int(cfg["min_chunk_tokens"])
    start, end = doc.content_start, doc.content_end

    hs = sorted((h for h in doc.headings if start <= h.offset < end), key=lambda h: (h.offset, h.level))
    dedup = []
    for h in hs:  # several TOC entries pointing at the same spot: keep the outermost
        if dedup and h.offset - dedup[-1].offset < 40:
            continue
        dedup.append(h)
    hs = dedup

    sections = []  # [title, kind, start, end]
    if len(hs) >= 2:
        level = _pick_level(doc, hs, chunk_tokens)
        hs = [h for h in hs if h.level <= level]
        log.info("Chunking by %s headings (level ≤ %d): %d sections", doc.heading_source, level, len(hs))
        if est_tokens(doc.text[start:hs[0].offset]) >= 400:
            sections.append(["Opening", "front", start, hs[0].offset])
        for a, b in zip(hs, hs[1:] + [None]):
            sections.append([a.title, classify(a.title), a.offset, b.offset if b else end])
    else:
        log.info("No usable chapter structure; using fixed ~%d-token chunks", chunk_tokens)
        sections.append(["Section", "chapter", start, end])

    # Drop front/back matter.
    kept = []
    for title, kind, a, b in sections:
        body = doc.text[a:b]
        if kind == "skip" or looks_like_index_or_notes(body):
            log.info("  skip: %s", title)
            continue
        b2 = trim_index_tail(doc.text, a, b)
        if b2 != b:
            log.info("  trimmed index-like tail from: %s", title)
        kept.append([title, kind, a, b2])

    # Fold heading-only sections (e.g. a "Part One" title page) into the next one.
    folded = []
    carry = None
    for sec in kept:
        if carry is not None and carry[1] == sec[2]:
            sec[2] = carry[0]
        carry = None
        if est_tokens(doc.text[sec[2]:sec[3]]) < 150 and sec is not kept[-1]:
            carry = (sec[2], sec[3])
            continue
        folded.append(sec)

    # Merge small sections with a neighbour.
    merged: list = []
    for title, kind, a, b in folded:
        item = {"titles": [title], "kind": kind, "start": a, "end": b}
        if merged:
            prev = merged[-1]
            prev_t = est_tokens(doc.text[prev["start"]:prev["end"]])
            cur_t = est_tokens(doc.text[a:b])
            if (cur_t < min_tokens or prev_t < min_tokens) and prev_t + cur_t <= chunk_tokens \
                    and prev["end"] == a:
                prev["titles"].append(title)
                prev["end"] = b
                if prev["kind"] != "chapter" and kind == "chapter":
                    prev["kind"] = "chapter"
                continue
        merged.append(item)

    # Split oversize sections at paragraph boundaries.
    chunks: list = []
    for item in merged:
        a, b = item["start"], item["end"]
        title = " · ".join(item["titles"])
        n = math.ceil(est_tokens(doc.text[a:b]) / chunk_tokens)
        spans = _split_spans(doc.text, a, b, n) if n > 1 else [(a, b)]
        for k, (s, e) in enumerate(spans):
            t = f"{title} (part {k + 1}/{len(spans)})" if len(spans) > 1 else title
            chunks.append(Chunk(index=len(chunks) + 1, title=t, kind=item["kind"], start=s, end=e,
                                tokens=est_tokens(doc.text[s:e]), page_first=doc.page_at(s),
                                page_last=doc.page_at(max(s, e - 1)), titles=item["titles"]))
    log.info("Chunks: %d (%s tokens total)", len(chunks), f"{sum(c.tokens for c in chunks):,}")
    return chunks


def _split_spans(text: str, a: int, b: int, n: int) -> list:
    """Split text[a:b] into n roughly equal spans, cutting only at paragraph (or sentence) breaks."""
    breaks = [m.end() for m in re.finditer(r"\n\s*\n", text[a:b])]
    if len(breaks) < n * 3:
        breaks = [m.end() for m in re.finditer(r"(?<=[.!?][\"”’)]?)\s+", text[a:b])]
    breaks = [a + x for x in breaks]
    spans, s = [], a
    for k in range(1, n):
        target = a + (b - a) * k // n
        cut = min(breaks, key=lambda x: abs(x - target)) if breaks else target
        if cut <= s:
            continue
        spans.append((s, cut))
        s = cut
    spans.append((s, b))
    return spans


def chunk_text_for_llm(doc: Doc, c: Chunk) -> str:
    """Chunk text with [p. N] markers at page boundaries so the model can cite pages."""
    if not doc.pages:
        return doc.text[c.start:c.end]
    out, pos = [], c.start
    first = doc.page_at(c.start)
    if first:
        out.append(f"[p. {first}]\n")
    for off, label in doc.pages:
        if c.start < off < c.end:
            out.append(doc.text[pos:off])
            out.append(f"[p. {label}] ")
            pos = off
    out.append(doc.text[pos:c.end])
    return "".join(out)


def chunks_to_json(chunks: list) -> list:
    return [asdict(c) for c in chunks]


def chunks_from_json(data: list) -> list:
    return [Chunk(**c) for c in data]
