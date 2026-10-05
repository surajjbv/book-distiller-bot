"""Book-level synthesis from chapter notes, then a compact audit that returns only corrections.

Speed: every call starts with the same prefix (system + book + notes + style), so LM Studio reuses the
already-read prompt; the two parts that don't depend on each other run in parallel; and the audit returns
a short list of fixes instead of rewriting the whole summary.
"""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from .extract import S, _obj, arr, text
from .llm import LMStudio
from .util import est_tokens, log, pmap, read_json, write_json

REFS = arr(S, 1, 6)
PARTS = {
    "overview": (_obj(
        tldr=arr(text(20), 5, 5),
        core_thesis=text(200),
        themes=arr(_obj(name=text(), explanation=text(60), chapters=REFS), 3, 5),
    ), """Write the book-level overview:
- tldr: exactly 5 bullets capturing what a reader must know.
- core_thesis: the author's central argument in one tight paragraph: what they claim, why it matters, and the mechanism.
- themes: 3–5 cross-chapter themes. For each: a short name, an explanation that connects how the idea develops across chapters, and the chapter refs it draws on (e.g. "Ch 3")."""),
    "ideas": (_obj(
        frameworks=arr(_obj(name=text(), what=text(40), how_to_apply=text(30), chapters=REFS), 0, 6),
        key_stories=arr(_obj(title=text(), story=text(60), lesson=text(15), chapter=text()), 2, 6),
    ), """Consolidate the book's ideas:
- frameworks: the 3–6 most useful frameworks, models or methods across the book (merge duplicates). For each: name, what it is (with its steps/components), how to apply it, and chapter refs. Only frameworks present in the notes; fewer is fine.
- key_stories: the 3–6 most memorable stories, cases or examples: title, the story in 2–3 sentences, the lesson, and its chapter ref."""),
    "action": (_obj(
        takeaways=arr(_obj(action=text(15), why=text(15)), 5, 10),
        critique=_obj(strengths=arr(text(20), 2, 4), weaknesses=arr(text(20), 2, 4), missing=arr(text(20), 1, 3),
                      who_should_read=text(40)),
        cheat_sheet=arr(_obj(label=text(), line=text(10)), 8, 12),
    ), """Make it actionable and honest:
- takeaways: 6–10 concrete actions (start with a verb) and, in one sentence, why each works.
- critique: strengths (2–4), weaknesses (2–4: weak evidence, overreach, contradictions, datedness), missing (1–3 gaps or counter-arguments the book ignores), who_should_read (who benefits, who can skip it).
- cheat_sheet: 10–12 one-line entries that fit on one printed page: a short label and a single line."""),
}
SHARE = {"overview": 0.25, "ideas": 0.35, "action": 0.40}  # split of the overview's word budget
SUMMARY_SCHEMA = _obj(**{k: v for schema, _ in PARTS.values() for k, v in schema["properties"].items()})
LIST_FIELDS = ["tldr", "themes", "frameworks", "key_stories", "takeaways", "cheat_sheet",
               "critique.strengths", "critique.weaknesses", "critique.missing"]
TEXT_FIELDS = ["core_thesis", "critique.who_should_read"]
FIX_SCHEMA = _obj(fixes=arr(_obj(
    field={"type": "string", "enum": LIST_FIELDS + TEXT_FIELDS},
    action={"type": "string", "enum": ["replace", "remove", "add"]},
    index={"type": "integer"},
    problem=S,
    value=S,
), 0, 12))
GROUP_SCHEMA = _obj(overview=text(100), themes=arr(text(10), 1, 6), evidence=arr(text(10), 0, 8),
                    takeaways=arr(text(10), 1, 8), frameworks=arr(_obj(name=text(), description=text(20), chapters=REFS), 0, 8),
                    stories=arr(_obj(title=text(), lesson=text(10), chapter=text()), 0, 6))
SYSTEM = ("You are an expert editor writing an executive summary of a non-fiction book from verified chapter "
          "notes. Use only what the notes support; never add outside facts. Follow the style guide exactly. "
          "Plain text inside JSON strings: no markdown except **bold**, no HTML. Return JSON only.")


def digest(note: dict, full: bool) -> str:
    """Plain-text digest of one chapter's notes (full or compact)."""
    lines = [f"[Ch {note['_index']}] {note['_title']}" + (f" ({note['_pages']})" if note.get("_pages") else ""),
             f"Core idea: {note.get('core_idea', '')}"]
    if full:
        lines.append(f"Summary: {note.get('summary', '')}")
    lines += [f"- {a}" for a in note.get("key_arguments", [])[: 6 if full else 4]]
    for f in note.get("frameworks", []):
        lines.append(f"Framework — {f['name']}: {f['description']}" + (f" Steps: {'; '.join(f['steps'])}" if full and f.get("steps") else ""))
    if full:
        lines += [f"Data: {d['fact']} ({d['context']})" for d in note.get("data_points", [])[:4]]
    lines += [f"Story — {e['title']}: " + (f"{e['story']} " if full else "") + f"Lesson: {e['lesson']}" for e in note.get("examples", [])]
    lines += [f"Takeaway: {t}" for t in note.get("takeaways", [])[: 5 if full else 3]]
    return "\n".join(lines)


def _field(obj: dict, path: str):
    """(container, key) for a dotted field path like 'critique.strengths'."""
    *parents, key = path.split(".")
    for p in parents:
        obj = obj.setdefault(p, {})
    return obj, key


def _item_schema(path: str) -> dict:
    s = SUMMARY_SCHEMA
    for p in path.split("."):
        s = s["properties"][p]
    return s.get("items", s)


def apply_fixes(draft: dict, fixes: list) -> tuple:
    """Apply the audit's corrections. Invalid fixes are skipped. Returns (summary, applied fixes)."""
    out, applied = json.loads(json.dumps(draft)), []
    order = {"replace": 0, "remove": 1, "add": 2}  # removals go from the end so indexes stay valid
    for f in sorted(fixes, key=lambda f: (order[f["action"]], -f["index"])):
        box, key = _field(out, f["field"])
        try:
            if f["field"] in TEXT_FIELDS:
                if f["action"] != "replace" or not f["value"]:
                    continue
                box[key] = f["value"]
            else:
                items, schema = box.setdefault(key, []), _item_schema(f["field"])
                value = None
                if f["action"] != "remove":
                    value = f["value"] if schema.get("type") == "string" else json.loads(f["value"])
                    jsonschema.validate(value, schema)
                if f["action"] == "add":
                    items.append(value)
                elif 0 <= f["index"] < len(items):
                    if f["action"] == "remove":
                        items.pop(f["index"])
                    else:
                        items[f["index"]] = value
                else:
                    continue
        except (json.JSONDecodeError, jsonschema.ValidationError, TypeError):
            continue
        applied.append(f)
    return out, applied


class Synthesizer:
    def __init__(self, llm: LMStudio, cfg: dict, style: str, title: str, author: str, workdir: Path,
                 words: int, tick=None):
        self.llm, self.style, self.dir = llm, style, workdir / "synthesis"
        self.book = f'"{title}"' + (f" by {author}" if author else "")
        self.tick = tick or (lambda *_: None)
        self.parallel = int(cfg.get("parallel", 2))
        self.words = words
        # One notes budget that also fits the audit (notes + full draft + fixes) so every call shares the prefix.
        self.budget = int(cfg["context_length"]) - int(cfg["max_output_tokens"]) - 2500 - est_tokens(style) - 1200

    # ------------------------------------------------------------ source notes

    def source(self, notes: list) -> str:
        for full in (True, False):
            text = "\n\n".join(digest(n, full) for n in notes)
            if est_tokens(text) <= self.budget:
                return text
        return self._condense(notes)

    def _condense(self, notes: list) -> str:
        """Hierarchical reduce: condense groups of chapters until the digest fits."""
        log.info("Notes exceed the context window; condensing in groups")
        texts, labels, rnd = [digest(n, True) for n in notes], [f"Ch {n['_index']}" for n in notes], 0
        while est_tokens("\n\n".join(texts)) > self.budget and len(texts) > 1:
            rnd += 1
            groups, cur = [], []
            for t, lab in zip(texts, labels):
                if cur and est_tokens("\n\n".join(x for x, _ in cur + [(t, lab)])) > self.budget:
                    groups.append(cur)
                    cur = []
                cur.append((t, lab))
            groups.append(cur)

            def condense(arg):
                gi, group = arg
                label = group[0][1].split("–")[0] + ("–" + group[-1][1].split("–")[-1].replace("Ch ", "") if len(group) > 1 else "")
                path = self.dir / f"group_r{rnd}_{gi:02d}.json"
                g = read_json(path)
                if g is None:
                    notes_text = "\n\n".join(t for t, _ in group)
                    g = self.llm.chat_json([{"role": "system", "content": SYSTEM}, {"role": "user", "content": (
                        f"Book: {self.book}\n\nCHAPTER NOTES:\n<<<\n{notes_text}\n>>>\n\nCondense these chapters into a dense "
                        "digest: overview of the argument, themes, every named framework (keep chapter refs like \"Ch 4\"), the "
                        "best stories with lessons and chapter refs, key evidence and numbers, and takeaways. Drop repetition.")}],
                        GROUP_SCHEMA, "group_digest")
                    write_json(path, g)
                lines = [f"[{label}]", f"Overview: {g['overview']}"] + [f"Theme: {t}" for t in g["themes"]]
                lines += [f"Framework — {f['name']} ({', '.join(f['chapters'])}): {f['description']}" for f in g["frameworks"]]
                lines += [f"Story — {s['title']} ({s['chapter']}): {s['lesson']}" for s in g["stories"]]
                lines += [f"Evidence: {e}" for e in g["evidence"]] + [f"Takeaway: {t}" for t in g["takeaways"]]
                return "\n".join(lines), label

            texts, labels = map(list, zip(*pmap(condense, list(enumerate(groups, 1)), self.parallel)))
        text = "\n\n".join(texts)
        return text[: int(self.budget * 3.6)]

    # ------------------------------------------------------------ synthesis + audit

    def _prefix(self, source: str) -> str:
        return (f"Book: {self.book}\n\nCHAPTER NOTES (verified; cite chapters as \"Ch 3\"):\n<<<\n{source}\n>>>\n\n"
                f"STYLE GUIDE:\n<<<\n{self.style}\n>>>\n\n")

    def _part(self, part: str, prefix: str, thesis: str) -> dict:
        path = self.dir / f"draft_{part}.json"
        d = read_json(path)
        if d is None:
            schema, task = PARTS[part]
            ctx = f"Core thesis already written (stay consistent): {thesis}\n\n" if thesis else ""
            limit = f"\n\nLength: at most {int(self.words * SHARE[part])} words in total for this section (hard limit)."
            d = self.llm.chat_json([{"role": "system", "content": SYSTEM},
                                    {"role": "user", "content": prefix + ctx + task + limit}], schema, f"synthesis_{part}")
            write_json(path, d)
            self.tick(1)
        return d

    def run(self, notes: list) -> tuple:
        """Returns (summary dict, list of audit fixes applied)."""
        prefix = self._prefix(self.source(notes))
        overview = self._part("overview", prefix, "")
        rest = pmap(lambda p: self._part(p, prefix, overview["core_thesis"]), ["ideas", "action"], self.parallel)
        draft = {**overview, **rest[0], **rest[1]}

        path = self.dir / "audit.json"
        fixes = read_json(path)
        if fixes is None:
            fixes = self.llm.chat_json([{"role": "system", "content": SYSTEM}, {"role": "user", "content": prefix + f"""DRAFT SUMMARY:
<<<
{json.dumps(draft, ensure_ascii=False)}
>>>

Audit the draft against the notes and list only the corrections needed:
1. Unsupported claims (not backed by the notes): replace or remove.
2. Contradictions with the notes or within the draft: replace.
3. Important ideas, frameworks or chapters the draft misses: add.
4. Wrong chapter refs, and style-guide violations (fluff, vagueness): replace.
Each fix: field, action (replace / remove / add), index (0-based item in that list; 0 for text fields and for add),
problem (one line), value (the new text; for lists of objects, the complete item as a JSON object string; "" for remove).
No fixes needed -> {{"fixes": []}}. Fix only real problems; do not rewrite what is already right."""}],
                FIX_SCHEMA, "audit", max_tokens=2000)["fixes"]
            write_json(path, fixes)
            self.tick(1)
        final, applied = apply_fixes(draft, fixes)
        log.info("Audit: %d fix(es) proposed, %d applied", len(fixes), len(applied))
        return final, applied
