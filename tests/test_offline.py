"""Offline tests (no LLM): run with  .venv/bin/python -m unittest discover tests"""
import unittest

from distiller.chunk import Chunk, _split_spans, classify, looks_like_index_or_notes, trim_index_tail
from distiller.extract import verify_quotes
from distiller.ingest import Doc
from distiller.llm import parse_json_loose, strip_think
from distiller.render import chapter_links, inline_md

TEXT = ("[front]\n\nThe first principle is that you must not fool yourself — and you are the easiest "
        "person to fool.\n\nSecond paragraph about “curly quotes” and things.\n\n" + "Filler sentence. " * 50)


def make_doc():
    return Doc(path="x.pdf", fmt="pdf", title="T", author="A", text=TEXT,
               pages=[[0, "1"], [60, "2"], [130, "3"]], content_end=len(TEXT))


class QuoteTests(unittest.TestCase):
    def setUp(self):
        self.doc = make_doc()
        self.chunk = Chunk(index=1, title="c", kind="chapter", start=0, end=len(TEXT), tokens=100)

    def check(self, quotes):
        return verify_quotes(self.doc, self.chunk, {"quotes": quotes}, 90)

    def test_exact_and_page(self):
        kept, st = self.check([{"text": "you must not fool yourself", "page": "9", "why": ""}])
        self.assertEqual(st["exact"], 1)
        self.assertEqual(kept[0]["page"], "1")  # page corrected from the source, not the model

    def test_fuzzy_corrected_to_source(self):
        kept, st = self.check([{"text": "you must not fool yourself - and you are the easiest person to fool",
                                "page": "", "why": ""}])
        self.assertEqual(st["kept"], 1)
        self.assertIn("—", kept[0]["text"])  # source wording restored

    def test_smart_quotes_match(self):
        kept, _ = self.check([{"text": 'Second paragraph about "curly quotes" and things', "page": "", "why": ""}])
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["page"], "2")

    def test_fabricated_dropped(self):
        kept, st = self.check([{"text": "Success is the sum of small efforts repeated every day.", "page": "", "why": ""}])
        self.assertEqual(kept, [])
        self.assertEqual(len(st["dropped"]), 1)

    def test_page_markers_ignored(self):
        kept, _ = self.check([{"text": "[p. 1] you must not fool yourself", "page": "", "why": ""}])
        self.assertEqual(len(kept), 1)


class ChunkTests(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(classify("Acknowledgments"), "skip")
        self.assertEqual(classify("Index"), "skip")
        self.assertEqual(classify("Notes"), "skip")
        self.assertEqual(classify("Notes on Strategy"), "chapter")
        self.assertEqual(classify("Introduction"), "front")
        self.assertEqual(classify("Chapter 12: Epilogue"), "back")
        self.assertEqual(classify("The Full Project Gutenberg License"), "skip")
        self.assertEqual(classify("3. The Power of Habit"), "chapter")

    def test_index_detection(self):
        index = "\n".join(f"term{i}, {i}, {i + 4}" for i in range(40))
        self.assertTrue(looks_like_index_or_notes(index))
        prose = "\n\n".join("A long paragraph of real prose that goes on for a while. " * 4 for _ in range(20))
        self.assertFalse(looks_like_index_or_notes(prose + "\n" + "\n".join(f"t{i}, {i}" for i in range(15))))

    def test_trim_index_tail(self):
        prose = "Real chapter text that ends here.\n\n" * 5
        idx = "Index\n" + "\n".join(f"word{i}, {i}, {i + 2}" for i in range(20))
        t = prose + idx
        self.assertEqual(t[:trim_index_tail(t, 0, len(t))], prose)

    def test_split_spans_on_paragraphs(self):
        text = "\n\n".join(f"Paragraph {i}. " + "word " * 50 for i in range(20))
        spans = _split_spans(text, 0, len(text), 3)
        self.assertEqual(len(spans), 3)
        self.assertEqual(spans[0][0], 0)
        self.assertEqual(spans[-1][1], len(text))
        for s, e in spans[1:]:
            self.assertTrue(text[s:].startswith("Paragraph"))


class LLMHelpers(unittest.TestCase):
    def test_strip_think(self):
        self.assertEqual(strip_think("<think>hmm</think>{\"a\":1}"), '{"a":1}')
        self.assertEqual(strip_think("reasoning</think>{}"), "{}")
        self.assertEqual(parse_json_loose("<think>x</think>```json\n{\"a\": 2}\n```"), {"a": 2})


class RenderHelpers(unittest.TestCase):
    def test_inline_md_escapes(self):
        self.assertEqual(str(inline_md("<b>x</b> **y**")), "&lt;b&gt;x&lt;/b&gt; <strong>y</strong>")

    def test_chapter_links(self):
        html = str(chapter_links(["Ch 2–3", "Ch 9"], {1: "", 2: "", 3: "Part%202.html"}))
        self.assertIn('href="#ch-2"', html)
        self.assertIn('href="Part%202.html#ch-3"', html)  # chapter in another part links to that file
        self.assertIn("Ch 9", html)


class PlanTests(unittest.TestCase):
    def test_split_parts_contiguous_and_balanced(self):
        from distiller.plan import split_parts
        parts = split_parts([500] * 20, 3, first_extra=2500)
        self.assertEqual(parts, sorted(parts))              # book order kept
        self.assertEqual(set(parts), {1, 2, 3})
        self.assertLess(parts.count(1), parts.count(3))     # part 1 also holds the overview, so fewer chapters
        self.assertEqual(split_parts([300, 300], 4, 0), [1, 2])  # never more parts than chapters
        self.assertEqual(split_parts([300] * 5, 1, 900), [1] * 5)

    def test_budgets(self):
        from distiller.plan import budgets
        per_chunk, synth = budgets({"minutes": 30}, [None] * 7)
        self.assertEqual(synth, 2600)
        self.assertGreater(per_chunk, 400)
        per_chunk, _ = budgets({"minutes": 120}, [None] * 20)
        self.assertGreater(per_chunk, 900)                  # long plan -> richer chapter notes

    def test_part_minutes(self):
        from distiller.plan import part_minutes
        self.assertEqual(part_minutes("Each part of a summary is a 20–30 minute read."), 30)
        self.assertEqual(part_minutes("no length here"), 30)


if __name__ == "__main__":
    unittest.main()


class FakeLLM:
    """Stands in for LM Studio: returns minimal schema-shaped objects."""
    model = "fake"

    def __init__(self):
        self.calls = []

    def chat_json(self, messages, schema, name, max_tokens=None):
        self.calls.append(name)

        def fill(s):
            t = s.get("type")
            if t == "object":
                return {k: fill(v) for k, v in s["properties"].items()}
            if t == "array":
                return [] if name == "audit" else [fill(s["items"])]
            if t == "integer":
                return 5
            if t == "boolean":
                return True
            return s.get("enum", ["**text** for " + name])[0]
        return fill(schema)


def big_notes(n=40):
    return [{"_index": i, "_title": f"Chapter {i}", "_pages": "", "core_idea": "idea " * 30,
             "summary": "summary words " * 120, "key_arguments": ["argument " * 25] * 6,
             "frameworks": [{"name": "F", "description": "d " * 30, "steps": ["s"] * 3}],
             "data_points": [], "examples": [{"title": "t", "story": "s " * 40, "lesson": "l"}],
             "takeaways": ["take " * 10] * 5, "quotes": []} for i in range(1, n + 1)]


class SynthTests(unittest.TestCase):
    CFG = {"context_length": 16384, "max_output_tokens": 3500, "parallel": 2}

    def test_hierarchical_reduce_audit_and_resume(self):
        import tempfile
        from pathlib import Path
        from distiller.synth import Synthesizer
        with tempfile.TemporaryDirectory() as d:
            llm = FakeLLM()
            final, fixes = Synthesizer(llm, self.CFG, "style", "T", "A", Path(d), words=2600).run(big_notes())
            self.assertIn("group_digest", llm.calls)        # notes too big: condensed in groups
            self.assertEqual(sum(c.startswith("synthesis_") for c in llm.calls), 3)
            self.assertEqual(llm.calls.count("audit"), 1)     # one compact audit call
            self.assertIn("tldr", final)
            self.assertIn("cheat_sheet", final)
            n = len(llm.calls)
            Synthesizer(llm, self.CFG, "style", "T", "A", Path(d), words=2600).run(big_notes())  # resumes from disk
            self.assertEqual(len(llm.calls), n)

    def test_apply_fixes(self):
        import json
        from distiller.synth import apply_fixes
        draft = {"tldr": ["a", "b", "c"], "core_thesis": "old", "critique": {"weaknesses": ["w1"]},
                 "frameworks": [{"name": "F", "what": "x", "how_to_apply": "y", "chapters": ["Ch 1"]}]}
        fixes = [
            {"field": "tldr", "action": "replace", "index": 1, "problem": "p", "value": "B is the corrected bullet text"},
            {"field": "tldr", "action": "replace", "index": 2, "problem": "too short", "value": "x"},
            {"field": "tldr", "action": "remove", "index": 0, "problem": "p", "value": ""},
            {"field": "core_thesis", "action": "replace", "index": 0, "problem": "p", "value": "new"},
            {"field": "critique.weaknesses", "action": "add", "index": 0, "problem": "p", "value": "w2: evidence is anecdotal only"},
            {"field": "frameworks", "action": "add", "index": 0, "problem": "p",
             "value": json.dumps({"name": "G", "what": "A framework described in enough words to pass", "how_to_apply": "Apply it weekly to every decision", "chapters": ["Ch 2"]})},
            {"field": "frameworks", "action": "add", "index": 0, "problem": "bad", "value": "{not json"},
            {"field": "tldr", "action": "remove", "index": 9, "problem": "out of range", "value": ""},
        ]
        out, applied = apply_fixes(draft, fixes)
        self.assertEqual(out["tldr"], ["B is the corrected bullet text", "c"])  # "x" rejected: too short
        self.assertEqual(out["core_thesis"], "new")
        self.assertEqual(out["critique"]["weaknesses"], ["w1", "w2: evidence is anecdotal only"])
        self.assertEqual([f["name"] for f in out["frameworks"]], ["F", "G"])
        self.assertEqual(len(applied), 5)
        self.assertEqual(draft["tldr"], ["a", "b", "c"])  # draft untouched


if __name__ == "__main__":
    unittest.main()


class PmapTests(unittest.TestCase):
    def test_first_error_cancels_queued_work(self):
        import time
        from distiller.util import pmap
        ran = []

        def work(i):
            ran.append(i)
            if i == 0:
                raise ValueError("boom")
            time.sleep(0.2)
            return i
        t = time.time()
        with self.assertRaises(ValueError):
            pmap(work, list(range(20)), 2)
        self.assertLess(time.time() - t, 1.0)   # did not wait for 20 items
        self.assertLess(len(ran), 5)            # queued items were cancelled

    def test_order_preserved(self):
        from distiller.util import pmap
        self.assertEqual(pmap(lambda x: x * x, [3, 1, 2], 2), [9, 1, 4])
