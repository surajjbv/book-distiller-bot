"""Resumable per-book pipeline: chunk → plan → extract (parallel) → verify → synthesize + audit → figures → render.

Every step writes its result to work/<book>/ as soon as it is done, so a crash resumes where it stopped.
"""
from __future__ import annotations

import math
import shutil
import time
from datetime import date
from pathlib import Path
from urllib.parse import quote

from tqdm import tqdm

from . import chunk as chunking
from . import ingest
from .extract import extract_chunk, verify_quotes
from .images import process_images
from .llm import LMStudio, tidy
from .plan import WPM, budgets, plan_book, split_parts
from .render import data_uri, render_summary, word_count
from .synth import Synthesizer
from .util import DistillerError, log, pmap, read_json, read_style, safe_filename, write_json

STEPS = ["chunk", "plan", "extract", "synthesize", "images", "render"]
ARTIFACTS = {"chunk": ["doc.json", "chunks.json"], "plan": ["plan.json"], "extract": ["notes", "verify.json"],
             "synthesize": ["synthesis", "synthesis.json"], "images": ["images", "images.json"], "render": []}


class BookJob:
    def __init__(self, path: Path, cfg: dict, llm: LMStudio, workdir: Path):
        self.path, self.cfg, self.llm, self.dir = Path(path), cfg, llm, Path(workdir)
        self.style = read_style()
        self.parallel = int(cfg.get("parallel", 2))
        self.bar: tqdm | None = None

    def reset_from(self, step: str) -> None:
        for s in STEPS[STEPS.index(step):]:
            for name in ARTIFACTS[s]:
                p = self.dir / name
                if p.is_dir():
                    shutil.rmtree(p)
                elif p.exists():
                    p.unlink()
        log.info("Redoing from step '%s'", step)

    def tick(self, n: float = 1) -> None:
        if self.bar is not None:
            if self.bar.n + n > self.bar.total:
                self.bar.total = self.bar.n + n + 1
            self.bar.update(n)

    # ------------------------------------------------------------ steps

    def load(self):
        d, c = read_json(self.dir / "doc.json"), read_json(self.dir / "chunks.json")
        if d and c is not None:
            return ingest.Doc.from_json(d), chunking.chunks_from_json(c)
        doc = ingest.load(self.path)
        chunks = chunking.build_chunks(doc, self.cfg)
        if not chunks:
            raise DistillerError(f"No content left in {self.path.name} after removing front/back matter.")
        write_json(self.dir / "doc.json", doc.to_json())
        write_json(self.dir / "chunks.json", chunking.chunks_to_json(chunks))
        for x in chunks:
            log.info("  %2d. %-58s %6s tok  %s", x.index, x.title[:58], x.tokens,
                     f"p.{x.page_first}–{x.page_last}" if x.page_first else x.kind)
        return doc, chunks

    def plan(self, doc, chunks) -> dict:
        p = read_json(self.dir / "plan.json")
        if p is None:
            p = plan_book(self.llm, doc, chunks, self.cfg, self.style)
            write_json(self.dir / "plan.json", p)
        log.info("Plan: %d-minute summary in %d part(s). %s", p["minutes"], p["parts"], p["reason"])
        return p

    def extract(self, doc, chunks, words: int) -> dict:
        def one(c):
            p = self.dir / "notes" / f"{c.index:02d}.json"
            rec = read_json(p)
            if rec is None:
                t = time.time()
                rec = {"chunk": c.index, "title": c.title, "model": self.llm.model,
                       "notes": extract_chunk(self.llm, doc, c, len(chunks), self.style, words)}
                write_json(p, rec)
                log.debug("chunk %d extracted in %.0fs", c.index, time.time() - t)
                self.tick(1)
            return c.index, rec["notes"]

        return dict(pmap(one, sorted(chunks, key=lambda c: -c.tokens), self.parallel))  # largest first: slots finish together

    def verify(self, doc, chunks, raw: dict) -> tuple:
        notes, report = {}, {"total": 0, "kept": 0, "exact": 0, "fuzzy": 0, "dropped": []}
        for c in chunks:
            n = dict(raw[c.index])
            n["quotes"], st = verify_quotes(doc, c, n, int(self.cfg["quote_match_threshold"]))
            for k in ("total", "kept", "exact", "fuzzy"):
                report[k] += st[k]
            report["dropped"] += [{"chunk": c.index, "quote": q} for q in st["dropped"]]
            n.update(_index=c.index, _title=c.title, _pages=_pages(c))
            notes[c.index] = n
        write_json(self.dir / "verify.json", report)
        log.info("Quotes verified: %d/%d kept (%d exact, %d corrected to the source wording)",
                 report["kept"], report["total"], report["exact"], report["fuzzy"])
        return notes, report

    # ------------------------------------------------------------ run

    def run(self) -> dict:
        t0 = time.time()
        doc, chunks = self.load()
        done = sum((self.dir / "notes" / f"{c.index:02d}.json").exists() for c in chunks)
        done += len(list((self.dir / "synthesis").glob("draft_*.json"))) + (self.dir / "synthesis" / "audit.json").exists()
        self.bar = tqdm(total=len(chunks) + 6, initial=done, unit="step", dynamic_ncols=True, desc=doc.title[:30],
                        bar_format="{desc}: {percentage:3.0f}%|{bar}| {n:.0f}/{total:.0f} [{elapsed}<{remaining}]")
        try:
            plan = self.plan(doc, chunks)
            chunk_words, synth_words = budgets(plan, chunks)
            notes, qreport = self.verify(doc, chunks, self.extract(doc, chunks, chunk_words))
            synth = read_json(self.dir / "synthesis.json")
            if synth is None:
                final, fixes = Synthesizer(self.llm, self.cfg, self.style, doc.title, doc.author, self.dir,
                                           words=synth_words, tick=self.tick).run([notes[c.index] for c in chunks])
                synth = {"synthesis": final, "audit_fixes": fixes}
                write_json(self.dir / "synthesis.json", synth)
            figures = read_json(self.dir / "images.json")
            if figures is None:
                figures = process_images(doc, chunks, notes, self.cfg, self.llm, self.dir / "images", tick=self.tick)
                write_json(self.dir / "images.json", figures)
            self.tick(1)
        finally:
            self.bar.close()
            self.bar = None

        pages = self.render(doc, chunks, notes, synth, figures, qreport, plan)
        words = sum(p["words"] for p in pages)
        log.info("Rendered %d part(s), %s words (~%d min read) in %.0fs · %s", len(pages), f"{words:,}",
                 math.ceil(words / WPM), time.time() - t0, self.llm.stats())
        return {"title": doc.title, "pages": pages, "read_minutes": math.ceil(words / WPM)}

    def render(self, doc, chunks, notes, synth, figures, qreport, plan) -> list:
        """One HTML page per part. Part 1 carries the whole-book overview; chapters follow in book order."""
        s = tidy(synth["synthesis"])
        book = {"tldr": s.get("tldr", []), "core_thesis": s.get("core_thesis", ""), "themes": s.get("themes", []),
                "frameworks": s.get("frameworks", []), "key_stories": s.get("key_stories", []),
                "takeaways": s.get("takeaways", []), "critique": s.get("critique", {}),
                "cheat_sheet": s.get("cheat_sheet", [])}
        chapters, quotes, figs = {}, {}, {}
        for c in chunks:
            n = notes[c.index]
            chapters[c.index] = {"index": c.index, "title": c.title, "kind": c.kind, "pages": n["_pages"],
                                 "core_idea": n.get("core_idea"), "summary": n.get("summary"),
                                 "key_arguments": n.get("key_arguments", []), "frameworks": n.get("frameworks", []),
                                 "data_points": n.get("data_points", []), "examples": n.get("examples", []),
                                 "takeaways": n.get("takeaways", [])}
            quotes[c.index] = [dict(q, chapter=c.title, chunk=c.index) for q in n["quotes"]]
        for f in figures:
            if (self.dir / "images" / f["file"]).exists():
                figs.setdefault(f["chunk"], []).append(dict(f, src=data_uri(self.dir / "images" / f["file"], f["mime"])))

        sizes = [word_count({"chapters": [chapters[c.index]], "quotes": quotes[c.index]}) for c in chunks]
        part_of = dict(zip([c.index for c in chunks], split_parts(sizes, plan["parts"], word_count(book))))
        n = max(part_of.values())
        names = [output_name(doc.title, k, n) for k in range(1, n + 1)]
        pages = []
        for k in range(1, n + 1):
            mine = [c for c in chunks if part_of[c.index] == k]
            pf = next((c.page_first for c in mine if c.page_first), None)
            pl = next((c.page_last for c in reversed(mine) if c.page_last), None)
            ctx = {
                "title": doc.title, "author": doc.author, "generated": date.today().isoformat(),
                "source_file": self.path.name, "model": self.llm.model, "quote_stats": qreport,
                "page_range": f"pp. {pf}–{pl}" if pf and pl else "",
                "part": k, "parts": n, "show_book": k == 1,
                "nav": [{"n": j, "href": quote(names[j - 1]), "current": j == k} for j in range(1, n + 1)],
                "chapter_href": {i: ("" if p == k else quote(names[p - 1])) for i, p in part_of.items()},
                "chapters": [chapters[c.index] for c in mine],
                "quotes": [q for c in mine for q in quotes[c.index]],
                "figures": [f for c in mine for f in figs.get(c.index, [])],
                "audit_fixes": synth.get("audit_fixes", []) if k == 1 else [],
                **(book if k == 1 else {key: [] for key in book}),
            }
            ctx["word_count"] = word_count(ctx)
            ctx["read_minutes"] = max(1, math.ceil(ctx["word_count"] / WPM))
            pages.append({"name": names[k - 1], "html": render_summary(ctx), "words": ctx["word_count"],
                          "minutes": ctx["read_minutes"]})
        return pages


def _pages(c) -> str:
    if not c.page_first:
        return ""
    return f"p. {c.page_first}" if c.page_first == c.page_last else f"pp. {c.page_first}–{c.page_last}"


def output_name(title: str, part: int = 1, parts: int = 1) -> str:
    suffix = f" (Part {part} of {parts})" if parts > 1 else ""
    return f"{safe_filename(title)} – Summary{suffix}.html"
