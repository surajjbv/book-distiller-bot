"""Load a PDF or EPUB into a `Doc`: one text string plus page map, headings and image refs.

Everything downstream works with character offsets into `Doc.text`, so chunking,
quote verification and figure placement share one coordinate system.
"""
from __future__ import annotations

import bisect
import posixpath
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import unquote

from .util import DistillerError, log

GUTENBERG_START = re.compile(r"\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG[^\n]*", re.I)
GUTENBERG_END = re.compile(r"\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG", re.I)


@dataclass
class Heading:
    offset: int
    title: str
    level: int
    source: str  # toc | nav | heading | spine


@dataclass
class Doc:
    path: str
    fmt: str
    title: str
    author: str
    text: str
    pages: list = field(default_factory=list)       # [[offset, label], ...] sorted by offset
    headings: list = field(default_factory=list)    # [Heading]
    images: list = field(default_factory=list)      # EPUB only: [[offset, href]]
    content_start: int = 0
    content_end: int = 0
    heading_source: str = ""

    def page_at(self, offset: int) -> str | None:
        if not self.pages:
            return None
        i = bisect.bisect_right([p[0] for p in self.pages], offset) - 1
        return self.pages[max(i, 0)][1]

    def to_json(self) -> dict:
        d = asdict(self)
        d["headings"] = [asdict(h) if isinstance(h, Heading) else h for h in self.headings]
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Doc":
        d = dict(d)
        d["headings"] = [Heading(**h) for h in d.get("headings", [])]
        return cls(**d)


def load(path: Path) -> Doc:
    ext = path.suffix.lower()
    if ext == ".pdf":
        doc = load_pdf(path)
    elif ext == ".epub":
        doc = load_epub(path)
    else:
        raise DistillerError(f"Unsupported file type: {path.name} (PDF and EPUB only)")
    _apply_gutenberg_range(doc)
    log.info("Loaded %s: %s chars, %d pages, %d headings (%s)", path.name, f"{len(doc.text):,}",
             len(doc.pages), len(doc.headings), doc.heading_source or "none")
    return doc


def _apply_gutenberg_range(doc: Doc) -> None:
    doc.content_start, doc.content_end = 0, len(doc.text)
    m = GUTENBERG_START.search(doc.text)
    if m:
        doc.content_start = m.end()
    m = GUTENBERG_END.search(doc.text, doc.content_start)
    if m:
        doc.content_end = m.start()


def _title_pattern(title: str):
    words = re.findall(r"\w+", title)[:8]
    if not words:
        return None
    return re.compile(r"\W*".join(re.escape(w) for w in words), re.I)


def _clean_meta(s: str | None) -> str:
    s = (s or "").strip()
    if not s or s.lower() in {"untitled", "unknown", "none"} or re.search(r"\.(docx?|indd|pdf|qxd)$", s, re.I):
        return ""
    return s


# ====================================================================== PDF

def load_pdf(path: Path) -> Doc:
    import fitz

    d = fitz.open(path)
    if d.needs_pass:
        raise DistillerError(f"{path.name} is password-protected. Remove the password and try again.")
    flags = (fitz.TEXTFLAGS_TEXT & ~fitz.TEXT_PRESERVE_LIGATURES) | fitz.TEXT_DEHYPHENATE
    n = len(d)

    # Pass 1: blocks per page, and find running headers/footers to drop.
    page_blocks = []
    edge_counts: Counter = Counter()
    for page in d:
        h = page.rect.height or 1
        blocks = []
        for b in page.get_text("blocks", flags=flags):
            if b[6] != 0:
                continue
            txt = re.sub(r"\s*\n\s*", " ", b[4]).strip()
            if not txt:
                continue
            edge = b[1] < h * 0.08 or b[3] > h * 0.92
            key = re.sub(r"\d+", "#", txt.lower()) if edge else None
            if key:
                edge_counts[key] += 1
            blocks.append((txt, key))
        page_blocks.append(blocks)
    running = {k for k, c in edge_counts.items() if c >= max(3, n * 0.2)}

    # Printed page labels (e.g. roman numerals for front matter) when they look sane, else physical numbers.
    labels = [d[i].get_label() for i in range(n)]
    if not all(labels) or len(set(labels)) < n * 0.95:
        labels = [str(i + 1) for i in range(n)]

    parts, pages, off = [], [], 0
    for i, blocks in enumerate(page_blocks):
        kept = [t for t, k in blocks if k not in running and not re.fullmatch(r"[\divxlcIVXLC]{1,6}", t)]
        page_text = "\n\n".join(kept)
        pages.append([off, labels[i]])
        parts.append(page_text)
        off += len(page_text) + 2
    text = "\n\n".join(parts)

    chars = sum(len(p) for p in parts)
    if chars < max(200, n * 40):
        raise DistillerError(
            f"{path.name} has no usable text layer (looks scanned: {chars} chars over {n} pages).\n"
            f"  Fix: OCR it first, e.g.  brew install ocrmypdf && ocrmypdf --skip-text \"{path}\" \"{path.with_stem(path.stem + '-ocr')}\"\n"
            "  then drop the OCR'd PDF into input/.")
    empty = sum(1 for p in parts if len(p) < 20)
    if empty > n * 0.5:
        log.warning("%d of %d pages have no text; parts of this PDF may be scanned images (consider ocrmypdf).", empty, n)

    meta = d.metadata or {}
    doc = Doc(path=str(path), fmt="pdf", title=_clean_meta(meta.get("title")) or _pretty_stem(path),
              author=_clean_meta(meta.get("author")), text=text, pages=pages)

    def offset_for(page_idx: int, title: str) -> int:
        start = pages[page_idx][0]
        pat = _title_pattern(title)
        m = pat.search(parts[page_idx]) if pat else None
        return start + (m.start() if m else 0)

    toc = [t for t in d.get_toc(simple=True) if 1 <= t[2] <= n]
    if len(toc) >= 3:
        doc.headings = [Heading(offset_for(p - 1, t), t.strip(), lvl, "toc") for lvl, t, p in toc]
        doc.heading_source = "pdf-toc"
    else:
        doc.headings = _pdf_font_headings(d, flags, offset_for)
        doc.heading_source = "pdf-headings" if doc.headings else ""
    return doc


CHAPTER_RE = re.compile(
    r"^(chapter|part|book|section|lesson|step)\s+([0-9]+|[ivxlcdm]+|one|two|three|four|five|six|seven|"
    r"eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)\b",
    re.I)


def _pdf_font_headings(d, flags, offset_for) -> list:
    """Fallback when there is no embedded TOC: chapter-word regex first, then font size."""
    lines = []  # (page_idx, text, size)
    sizes: Counter = Counter()
    for i, page in enumerate(d):
        for b in page.get_text("dict", flags=flags)["blocks"]:
            if b.get("type") != 0:
                continue
            for ln in b["lines"]:
                txt = "".join(s["text"] for s in ln["spans"]).strip()
                if not txt:
                    continue
                size = round(max(s["size"] for s in ln["spans"]), 1)
                sizes[size] += len(txt)
                lines.append((i, txt, size))
    if not sizes:
        return []
    body = sizes.most_common(1)[0][0]

    def subtitle(idx):  # e.g. "CHAPTER 3" followed by "THE TITLE" on the same page
        if idx + 1 < len(lines):
            p, t, s = lines[idx + 1]
            if p == lines[idx][0] and s >= body * 1.15 and len(t) < 90 and not CHAPTER_RE.match(t):
                return t
        return ""

    hits = [(k, l) for k, l in enumerate(lines) if CHAPTER_RE.match(l[1]) and len(l[1]) < 90]
    if len(hits) >= 3:
        out = []
        for k, (p, t, s) in hits:
            sub = subtitle(k)
            title = f"{t} — {sub}" if sub else t
            level = 0 if t.lower().startswith("part") else 1
            out.append(Heading(offset_for(p, t), title, level, "heading"))
        return out

    big = [(k, l) for k, l in enumerate(lines)
           if l[2] >= body * 1.25 and len(l[1]) < 100 and re.search(r"[A-Za-z]{3}", l[1])]
    by_size = Counter(l[2] for _, l in big)
    for size in sorted(by_size, reverse=True):
        if 3 <= by_size[size] <= max(3, len(d) * 0.6):
            sel = [(k, l) for k, l in big if abs(l[2] - size) < 0.6]
            out, last = [], (-1, -9)
            for k, (p, t, s) in sel:
                if p == last[0] and k - last[1] <= 2 and out:  # multi-line heading
                    out[-1].title += " " + t
                else:
                    out.append(Heading(offset_for(p, t), t, 1, "heading"))
                last = (p, k)
            return out
    return []


def _pretty_stem(path: Path) -> str:
    s = re.sub(r"[_]+", " ", path.stem)
    s = re.sub(r"\s*[-–]\s*(ocr|epub|pdf)$", "", s, flags=re.I)
    return s.strip() or path.stem


# ====================================================================== EPUB

BLOCK_TAGS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "section", "article",
              "tr", "br", "pre", "figure", "figcaption", "dt", "dd", "table", "ul", "ol", "header",
              "footer", "aside", "nav", "hr", "body"}
SKIP_TAGS = {"script", "style", "head", "title", "svg", "math", "noscript"}


class _TextBuilder:
    def __init__(self):
        self.parts: list = []
        self.n = 0
        self.last = "\n"

    def text(self, s: str) -> None:
        s = re.sub(r"\s+", " ", s)
        if self.last in " \n":
            s = s.lstrip()
        if s:
            self.parts.append(s)
            self.n += len(s)
            self.last = s[-1]

    def block(self) -> None:
        if self.n and self.last != "\n":
            if self.last == " ":
                self.parts[-1] = self.parts[-1][:-1]
                self.n -= 1
            self.parts.append("\n\n")
            self.n += 2
            self.last = "\n"


def _walk_html(html: bytes, tb: _TextBuilder, on_anchor, on_page, on_image) -> None:
    import warnings

    from bs4 import BeautifulSoup, Comment, NavigableString, Tag, XMLParsedAsHTMLWarning
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)  # XHTML parses fine as HTML

    soup = BeautifulSoup(html, "lxml")
    root = soup.body or soup

    def is_pagebreak(tag) -> str | None:
        et = (tag.get("epub:type") or "") + " " + (tag.get("role") or "")
        cls = " ".join(tag.get("class") or [])
        if "pagebreak" in et or "pagenum" in cls or "page-number" in cls:
            label = tag.get("title") or tag.get("aria-label") or tag.get_text(" ", strip=True) or tag.get("id") or ""
            label = re.sub(r"^(page|pg\.?|p\.)\s*|[\[\]{}]", "", label.strip(), flags=re.I).strip()
            label = re.sub(r"^Page_", "", label)
            return label or None
        return None

    def walk(node):
        for child in node.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                tb.text(str(child))
                continue
            if not isinstance(child, Tag) or child.name in SKIP_TAGS:
                continue
            for attr in ("id", "name"):
                if child.get(attr):
                    on_anchor(child[attr], tb.n)
            label = is_pagebreak(child)
            if label is not None:
                on_page(label, tb.n)
                continue
            if child.name in ("img", "image"):
                src = child.get("src") or child.get("xlink:href") or child.get("href")
                if src:
                    on_image(src, tb.n)
            block = child.name in BLOCK_TAGS
            if block:
                tb.block()
            walk(child)
            if block:
                tb.block()

    walk(root)
    tb.block()


def load_epub(path: Path) -> Doc:
    import ebooklib
    from ebooklib import epub

    try:
        book = epub.read_epub(str(path), {"ignore_ncx": False})
    except Exception as e:
        raise DistillerError(f"Could not open {path.name} as EPUB: {e}")

    tb = _TextBuilder()
    doc_start: dict = {}
    anchors: dict = {}
    pages: list = []
    images: list = []
    spine_titles: list = []

    for idref, *_ in book.spine:
        item = book.get_item_with_id(idref)
        if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        name = item.get_name()
        tb.block()
        doc_start[name] = tb.n
        base = posixpath.dirname(name)
        _walk_html(
            item.get_content(), tb,
            on_anchor=lambda a, o, name=name: anchors.setdefault((name, a), o),
            on_page=lambda label, o: pages.append([o, label]),
            on_image=lambda src, o, base=base: images.append(
                [o, posixpath.normpath(posixpath.join(base, unquote(src.split("#")[0])))]),
        )
        heading = re.search(rb"<h[1-3][^>]*>(.*?)</h[1-3]>", item.get_content(), re.S | re.I)
        if heading:
            spine_titles.append((doc_start[name], re.sub(r"<[^>]+>|\s+", " ", heading.group(1).decode("utf-8", "ignore")).strip()))
    text = "".join(tb.parts)

    def resolve(href: str) -> int | None:
        href = unquote(href or "")
        file, _, frag = href.partition("#")
        cands = [n for n in doc_start if n == file or n.endswith("/" + file) or posixpath.basename(n) == posixpath.basename(file)]
        if not cands:
            return None
        name = cands[0]
        if frag and (name, frag) in anchors:
            return anchors[(name, frag)]
        return doc_start[name]

    headings: list = []

    def walk_toc(entries, level):
        for e in entries:
            if isinstance(e, tuple):
                sec, children = e
                off = resolve(getattr(sec, "href", "") or "")
                if off is not None and getattr(sec, "title", ""):
                    headings.append(Heading(off, sec.title.strip(), level, "nav"))
                walk_toc(children, level + 1)
            elif isinstance(e, list):
                walk_toc(e, level)
            else:
                off = resolve(getattr(e, "href", ""))
                if off is not None and getattr(e, "title", ""):
                    headings.append(Heading(off, e.title.strip(), level, "nav"))

    walk_toc(book.toc, 1)
    source = "epub-nav"
    if len(headings) < 2:
        headings = [Heading(o, t, 1, "spine") for o, t in spine_titles if t]
        source = "epub-spine" if headings else ""

    def meta(field_name):
        v = book.get_metadata("DC", field_name)
        return v[0][0] if v else ""

    pages.sort(key=lambda p: p[0])
    return Doc(path=str(path), fmt="epub", title=_clean_meta(meta("title")) or _pretty_stem(path),
               author=_clean_meta(meta("creator")), text=text, pages=pages,
               headings=sorted(headings, key=lambda h: h.offset), images=images, heading_source=source)
