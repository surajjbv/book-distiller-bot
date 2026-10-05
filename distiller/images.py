"""Figure extraction (PDF/EPUB), heuristic filtering, and optional vision-model screening + captions."""
from __future__ import annotations

import base64
import io
import re
from pathlib import Path

from PIL import Image

from .extract import _obj, text
from .ingest import Doc
from .llm import LMStudio
from .util import log, pmap

VISION_SCHEMA = _obj(
    kind={"type": "string", "enum": ["diagram", "chart", "table", "infographic", "map", "photo",
                                     "illustration", "decorative", "text", "other"]},
    meaningful={"type": "boolean"},
    importance={"type": "integer", "minimum": 1, "maximum": 10},
    caption=text(10),
)
THINKING = re.compile(r"^(the user|we need|i need|let me|okay|ok,|need to|i should)\b", re.I)
KEEP_KINDS = {"diagram", "chart", "table", "infographic", "map"}


def _dhash(img: Image.Image) -> str:
    g = img.convert("L").resize((9, 8), Image.BILINEAR)
    px = list(g.getdata())
    bits = "".join("1" if px[r * 9 + c] > px[r * 9 + c + 1] else "0" for r in range(8) for c in range(8))
    return f"{int(bits, 2):016x}"


def _prepare(img: Image.Image, max_w: int) -> tuple:
    """Downscale and encode for embedding. Returns (bytes, mime)."""
    if img.mode in ("P", "LA", "RGBA") or img.mode == "1":
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, "white")
        bg.paste(img, mask=img.split()[-1])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    if img.width > max_w:
        img = img.resize((max_w, int(img.height * max_w / img.width)), Image.LANCZOS)
    colors = img.getcolors(4096)
    buf = io.BytesIO()
    if colors is not None:  # few colours: line art, charts → PNG stays crisp and small
        img.save(buf, "PNG", optimize=True)
        return buf.getvalue(), "image/png"
    img.save(buf, "JPEG", quality=82, optimize=True)
    return buf.getvalue(), "image/jpeg"


def candidates(doc: Doc, cfg: dict) -> list:
    """Return candidate figures: dicts with offset, page, img (PIL), area."""
    min_px = int(cfg.get("min_image_px", 160))
    out, seen = [], set()

    def accept(img: Image.Image, offset: int, page, area_frac: float) -> None:
        w, h = img.size
        if min(w, h) < min_px or w * h < min_px * min_px * 2:
            return
        if not (0.2 <= w / h <= 5):
            return
        dh = _dhash(img)
        if dh in seen:
            return
        seen.add(dh)
        out.append({"offset": offset, "page": page, "img": img, "area": w * h, "area_frac": area_frac})

    if doc.fmt == "pdf":
        import fitz
        d = fitz.open(doc.path)
        xref_pages: dict = {}
        for page in d:
            for im in page.get_images(full=True):
                xref_pages.setdefault(im[0], set()).add(page.number)
        for page in d:
            p_area = page.rect.width * page.rect.height or 1
            offset = doc.pages[page.number][0] if page.number < len(doc.pages) else 0
            label = doc.pages[page.number][1] if page.number < len(doc.pages) else str(page.number + 1)
            for im in page.get_images(full=True):
                xref = im[0]
                if len(xref_pages[xref]) > 2:  # logos, ornaments, backgrounds repeated across pages
                    continue
                rects = page.get_image_rects(xref)
                shown = max((r.width * r.height for r in rects), default=0) / p_area
                if shown and shown < 0.04:
                    continue
                try:
                    info = d.extract_image(xref)
                    pix = None
                    if info.get("smask") or info.get("colorspace", 3) not in (1, 3):
                        pix = fitz.Pixmap(d, xref)
                        if pix.alpha or pix.n - pix.alpha > 3:
                            pix = fitz.Pixmap(fitz.csRGB, pix)
                        img = Image.open(io.BytesIO(pix.tobytes("png")))
                    else:
                        img = Image.open(io.BytesIO(info["image"]))
                    img.load()
                except Exception as e:
                    log.debug("image xref %s on p.%s unreadable: %s", xref, label, e)
                    continue
                accept(img, offset, label, shown)
            _vector_figure(page, p_area, offset, label, accept)
    else:
        from ebooklib import epub
        book = epub.read_epub(doc.path, {"ignore_ncx": True})
        by_name = {it.get_name(): it for it in book.get_items()}
        counts: dict = {}
        for _, href in doc.images:
            counts[href] = counts.get(href, 0) + 1
        for offset, href in doc.images:
            item = by_name.get(href) or next((v for k, v in by_name.items() if k.endswith(href.split("/")[-1])), None)
            if item is None or counts[href] > 2 or href.lower().endswith(".svg"):
                continue
            try:
                img = Image.open(io.BytesIO(item.get_content()))
                img.load()
            except Exception:
                continue
            accept(img, offset, doc.page_at(offset), 0)
    return out


def _vector_figure(page, p_area, offset, label, accept) -> None:
    """Render clusters of vector drawings (charts/diagrams drawn as paths) as images."""
    try:
        drawings = page.get_drawings()
    except Exception:
        return
    rects = [d["rect"] for d in drawings if d["rect"].width * d["rect"].height < p_area * 0.8]
    if len(rects) < 25:
        return
    import fitz
    box = fitz.Rect(rects[0])
    for r in rects[1:]:
        box |= r
    frac = box.width * box.height / p_area
    if 0.08 <= frac <= 0.9:
        pix = page.get_pixmap(clip=box, dpi=150)
        accept(Image.open(io.BytesIO(pix.tobytes("png"))), offset, label, frac)


def chunk_for_offset(chunks: list, offset: int):
    for c in chunks:
        if c.start <= offset < c.end:
            return c
    return None


def process_images(doc: Doc, chunks: list, notes: dict, cfg: dict, llm: LMStudio, out_dir: Path, tick=None) -> list:
    """Screen and caption candidate figures with the model; return up to cfg.image_cap records."""
    tick = tick or (lambda *_: None)
    cands = [c for c in candidates(doc, cfg) if chunk_for_offset(chunks, c["offset"])]
    log.info("Figures: %d candidates after size/aspect/duplicate filtering", len(cands))
    if not cands:
        return []
    cands = sorted(cands, key=lambda c: -c["area"])[: int(cfg.get("image_candidates", 30))]

    def screen(c):
        ch = chunk_for_offset(chunks, c["offset"])
        data, mime = _prepare(c["img"], 1024)
        prompt = (f'This image is from the book "{doc.title}", chapter "{ch.title}"'
                  + (f" (page {c['page']})" if c["page"] else "")
                  + f". Chapter idea: {notes.get(ch.index, {}).get('core_idea', '')}\n\n"
                  "Classify it. meaningful = true only if it carries information a reader of a summary would want "
                  "(a diagram, chart, table, model, map or data). Decorative art, photos of people, ornaments, cover art "
                  "and images of plain text are not meaningful. importance: 1-10 for how much it helps explain the "
                  "book's ideas. caption: one or two sentences on what it shows and why it matters (no 'This image shows').")
        msgs = [{"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {
            "url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}}]}]
        try:
            v = llm.chat_json(msgs, VISION_SCHEMA, "figure", max_tokens=400)
            if THINKING.match(v["caption"]):  # image calls go through the chat endpoint, where Qwen may think aloud
                v = llm.chat_json(msgs, VISION_SCHEMA, "figure", max_tokens=400)
                if THINKING.match(v["caption"]):
                    v["caption"] = f"Figure from “{ch.title}”" + (f", p. {c['page']}" if c["page"] else "")
        except Exception as e:
            log.warning("Figure on p.%s skipped: %s", c["page"], e)
            v = {"meaningful": False}
        tick(0.25)
        keep = v["meaningful"] and (v["kind"] in KEEP_KINDS or (v["kind"] in ("photo", "illustration") and v["importance"] >= 8))
        return dict(c, chunk=ch.index, chapter=ch.title, caption=v["caption"], kind=v["kind"], importance=v["importance"]) if keep else None

    kept = [k for k in pmap(screen, cands, int(cfg.get("parallel", 2))) if k]
    kept = sorted(sorted(kept, key=lambda k: (-k["importance"], -k["area"]))[: int(cfg.get("image_cap", 15))],
                  key=lambda k: k["offset"])

    out_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for i, k in enumerate(kept, 1):
        data, mime = _prepare(k["img"], 1400)
        path = out_dir / f"fig_{i:02d}.{'png' if mime == 'image/png' else 'jpg'}"
        path.write_bytes(data)
        records.append({"file": path.name, "mime": mime, "page": k["page"], "chunk": k["chunk"],
                        "chapter": k["chapter"], "caption": k["caption"], "kind": k["kind"]})
    log.info("Figures kept: %d", len(records))
    return records
