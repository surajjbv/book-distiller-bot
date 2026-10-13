"""EPUB 3 / Kindle Scribe generator for book-distiller-bot."""
from __future__ import annotations

import html
import io
import uuid
from pathlib import Path

from ebooklib import epub

from .chunk import Chunk
from .ingest import Doc
from .llm import tidy
from .render import inline_md


def xml_md(s) -> str:
    """Escape for XHTML and allow the same inline markdown as the HTML summary."""
    return str(inline_md(s))


EPUB_CSS = """
body{font-family:Georgia,serif;margin:4%;line-height:1.6;color:#111;background:#fff}
h1,h2,h3,h4{font-weight:600;line-height:1.25;margin:1.4em 0 .5em;page-break-after:avoid;break-after:avoid}
h1{font-size:1.7em;border-bottom:1px solid #333;padding-bottom:.3em} h2{font-size:1.35em;border-bottom:1px solid #ccc;padding-bottom:.2em}
h3{font-size:1.15em} h4{font-size:1em;font-style:italic;color:#444}
p{margin:.6em 0} ul,ol{margin:.6em 0;padding-left:1.6em} li{margin:.35em 0}
blockquote{margin:1.2em 0;padding:.6em 1.2em;border-left:3px solid #555;background:#f7f7f7;font-style:italic}
blockquote .src{display:block;margin-top:.5em;font-size:.88em;font-style:normal;color:#555}
.card,.framework,.story{border:1px solid #bbb;border-radius:4px;padding:.9em 1.1em;margin:1em 0}
.core-idea{border-left:4px solid #111;padding:.8em 1em;margin:1em 0;background:#f4f4f4;font-weight:500}
.critique-block{border-left:3px solid #444;padding:.6em 1em;margin:.8em 0;background:#f9f9f9}
blockquote,.card,.framework,.story,.core-idea,figure{page-break-inside:avoid;break-inside:avoid}
figure{margin:1.5em 0;text-align:center} figure img{max-width:100%;height:auto}
figcaption{font-size:.88em;color:#444;margin-top:.6em;font-style:italic}
dl.cheat dt{font-weight:bold;margin-top:.8em} dl.cheat dd{margin:0 0 .8em 1.2em;color:#333}
.meta{font-size:.9em;color:#555;margin-bottom:1.5em}
"""


def _page(title: str, body: str) -> str:
    return (
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="en" lang="en">\n'
        '<head>\n'
        '<meta charset="utf-8" />\n'
        f"<title>{html.escape(title)}</title>\n"
        '<link rel="stylesheet" href="style/style.css" type="text/css" />\n'
        "</head>\n"
        f"<body>\n{body}\n</body>\n"
        "</html>"
    )


def build_epub(doc: Doc, chunks: list[Chunk], notes: dict, synth: dict, figures: list[dict], read_minutes: int,
               workdir: Path) -> bytes:
    """The same summary as the HTML (overview, chapters, frameworks, stories, takeaways, cheat sheet) as EPUB 3."""
    book = epub.EpubBook()
    book.set_identifier(f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_DNS, doc.title + (doc.author or ''))}")
    book.set_title(f"{doc.title} – Summary")
    book.set_language("en")
    if doc.author:
        book.add_author(doc.author)
    css = epub.EpubItem(uid="style", file_name="style/style.css", media_type="text/css", content=EPUB_CSS)
    book.add_item(css)
    s = tidy(synth.get("synthesis", {})) if isinstance(synth, dict) else {}
    toc = []

    def add(title: str, name: str, lines: list) -> None:
        item = epub.EpubHtml(title=title, file_name=name, lang="en")
        item.content = _page(title, "\n".join(lines))
        item.add_item(css)
        book.add_item(item)
        toc.append(item)

    def ul(items, tag="ul") -> str:
        return f"<{tag}>" + "".join(f"<li>{xml_md(i)}</li>" for i in items) + f"</{tag}>"

    figs: dict = {}
    for k, f in enumerate(figures or []):
        p = workdir / "images" / (f.get("file") or "")
        if f.get("file") and p.is_file():
            book.add_item(epub.EpubItem(uid=f"fig_{k}", file_name=f"images/{f['file']}",
                                        media_type=f.get("mime") or "image/png", content=p.read_bytes()))
            figs.setdefault(f.get("chunk"), []).append(f)

    # 1. Overview
    lines = [f"<h1>{xml_md(doc.title)}</h1>",
             '<p class="meta">' + (f"<strong>{xml_md(doc.author)}</strong> · " if doc.author else "")
             + f"{len(chunks)} sections · ~{read_minutes} min read</p>"]
    if s.get("tldr"):
        lines += ["<h2>TL;DR</h2>", ul(s["tldr"], "ol")]
    if s.get("core_thesis"):
        lines.append(f'<h2>Core thesis</h2><p>{xml_md(s["core_thesis"])}</p>')
    if s.get("themes"):
        lines.append("<h2>Themes</h2>")
        lines += [f'<div class="card"><h3>{xml_md(t.get("name"))}</h3><p>{xml_md(t.get("explanation"))}</p></div>'
                  for t in s["themes"]]
    cr = s.get("critique") or {}
    if cr:
        lines.append("<h2>Critique</h2>")
        if cr.get("who_should_read"):
            lines.append(f'<div class="card"><h3>Who should read it</h3><p>{xml_md(cr["who_should_read"])}</p></div>')
        for key, label in (("strengths", "Strengths"), ("weaknesses", "Weaknesses"), ("missing", "What's missing")):
            if cr.get(key):
                lines.append(f'<div class="critique-block"><h3>{label}</h3>{ul(cr[key])}</div>')
    add("Overview", "overview.xhtml", lines)

    # 2. Chapters
    for c in chunks:
        n = notes.get(c.index, {})
        lines = [f"<h1>{c.index}. {xml_md(c.title)}</h1>"]
        if n.get("_pages"):
            lines.append(f'<p class="meta">{xml_md(n["_pages"])}</p>')
        if n.get("core_idea"):
            lines.append(f'<div class="core-idea">{xml_md(n["core_idea"])}</div>')
        if n.get("summary"):
            lines.append(f'<p>{xml_md(n["summary"])}</p>')
        if n.get("key_arguments"):
            lines += ["<h3>Key arguments</h3>", ul(n["key_arguments"])]
        for fw in n.get("frameworks") or []:
            lines.append(f'<div class="framework"><h4>{xml_md(fw.get("name"))}</h4><p>{xml_md(fw.get("description"))}</p>'
                         + (ul(fw["steps"], "ol") if fw.get("steps") else "") + "</div>")
        if n.get("data_points"):
            lines.append("<h3>Data &amp; evidence</h3><ul>" + "".join(
                f'<li><strong>{xml_md(d.get("fact"))}</strong>' + (f' — {xml_md(d["context"])}' if d.get("context") else "")
                + "</li>" for d in n["data_points"]) + "</ul>")
        for ex in n.get("examples") or []:
            lines.append(f'<div class="story"><strong>{xml_md(ex.get("title"))}</strong><p>{xml_md(ex.get("story"))}</p>'
                         + (f'<p><em>Lesson:</em> {xml_md(ex["lesson"])}</p>' if ex.get("lesson") else "") + "</div>")
        if n.get("takeaways"):
            lines += ["<h3>Takeaways</h3>", ul(n["takeaways"])]
        for f in figs.get(c.index, []):
            pg = f' · p. {html.escape(str(f["page"]))}' if f.get("page") else ""
            lines.append(f'<figure><img src="images/{html.escape(f["file"])}" alt="{html.escape(f.get("caption") or "Figure")}" />'
                         f'<figcaption>{xml_md(f.get("caption"))}{pg}</figcaption></figure>')
        if n.get("quotes"):
            lines.append("<h3>Quotes</h3>")
            lines += [f'<blockquote>“{xml_md(q.get("text"))}”<span class="src">'
                      + (f'p. {xml_md(q["page"])}' if q.get("page") else xml_md(c.title)) + "</span></blockquote>"
                      for q in n["quotes"]]
        add(f"{c.index}. {c.title}", f"ch_{c.index:02d}.xhtml", lines)

    # 3. Book-level sections
    if s.get("frameworks"):
        add("Frameworks", "frameworks.xhtml", ["<h1>Frameworks</h1>"] + [
            f'<div class="framework"><h2>{xml_md(f.get("name"))}</h2><p>{xml_md(f.get("what"))}</p>'
            + (f'<p><strong>How to apply:</strong> {xml_md(f["how_to_apply"])}</p>' if f.get("how_to_apply") else "")
            + "</div>" for f in s["frameworks"]])
    if s.get("key_stories"):
        add("Key stories", "stories.xhtml", ["<h1>Key stories</h1>"] + [
            f'<div class="story"><h3>{xml_md(k.get("title"))}</h3><p>{xml_md(k.get("story"))}</p>'
            + (f'<p><em>Lesson:</em> {xml_md(k["lesson"])}</p>' if k.get("lesson") else "")
            + (f'<p class="meta">{xml_md(k["chapter"])}</p>' if k.get("chapter") else "") + "</div>"
            for k in s["key_stories"]])
    if s.get("takeaways"):
        add("Takeaways", "takeaways.xhtml", ["<h1>Takeaways</h1><ol>"] + [
            f'<li><strong>{xml_md(t.get("action"))}</strong>'
            + (f'<br/><em>Why:</em> {xml_md(t["why"])}' if t.get("why") else "") + "</li>"
            for t in s["takeaways"]] + ["</ol>"])
    if s.get("cheat_sheet"):
        add("Cheat sheet", "cheat_sheet.xhtml", ['<h1>Cheat sheet</h1><dl class="cheat">'] + [
            f'<dt>{xml_md(i.get("label"))}</dt><dd>{xml_md(i.get("line"))}</dd>' for i in s["cheat_sheet"]] + ["</dl>"])

    book.toc = tuple(toc)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", *toc]
    buf = io.BytesIO()
    epub.write_epub(buf, book)
    return buf.getvalue()
