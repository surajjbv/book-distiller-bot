"""End-to-end ingest on real files (no LLM): a PDF and an EPUB go through load() and build_chunks().

Fixtures in tests/fixtures/ are excerpts of James Allen's "As a Man Thinketh" (1903, public domain).
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from distiller.chunk import build_chunks
from distiller.ingest import load
from distiller.util import load_config

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CHAPTERS = ["Foreword", "Thought And Character", "Effect Of Thought On Circumstances"]


def config():
    cfg = load_config(FIXTURES / "no-config.json")  # defaults only, so a local config.json or .env can't change results
    cfg.update(chunk_tokens=600, min_chunk_tokens=200)  # small enough that the excerpt splits into several chunks
    return cfg


class FileIngestMixin:
    fmt = ""

    def setUp(self):
        self.doc = load(FIXTURES / f"as-a-man-thinketh-excerpt.{self.fmt}")
        self.chunks = build_chunks(self.doc, config())

    def test_metadata_and_text(self):
        self.assertEqual(self.doc.fmt, self.fmt)
        self.assertEqual(self.doc.title, "As a Man Thinketh")
        self.assertEqual(self.doc.author, "James Allen")
        self.assertIn("As a man thinketh in his heart so is he", self.doc.text)
        self.assertGreater(len(self.doc.pages), 3)

    def test_headings_from_toc(self):
        self.assertEqual([h.title for h in self.doc.headings], CHAPTERS)
        for h in self.doc.headings:
            self.assertTrue(self.doc.text[h.offset :].startswith(h.title), h.title)

    def test_chunks(self):
        c = self.chunks
        self.assertEqual(
            [x.title for x in c],
            [
                "Foreword",
                "Thought And Character (part 1/2)",
                "Thought And Character (part 2/2)",
                "Effect Of Thought On Circumstances",
            ],
        )
        self.assertEqual([x.kind for x in c], ["front", "chapter", "chapter", "chapter"])
        self.assertEqual([x.index for x in c], [1, 2, 3, 4])
        for a, b in zip(c, c[1:], strict=False):
            self.assertEqual(a.end, b.start)  # contiguous, no gaps or overlaps
        self.assertEqual(c[0].start, self.doc.headings[0].offset)
        self.assertEqual(c[-1].end, self.doc.content_end)
        for x in c:
            self.assertGreater(x.tokens, 100)
            self.assertIsNotNone(x.page_first)
            self.assertIsNotNone(x.page_last)


class PdfIngestTests(FileIngestMixin, unittest.TestCase):
    fmt = "pdf"


class EpubIngestTests(FileIngestMixin, unittest.TestCase):
    fmt = "epub"


if __name__ == "__main__":
    unittest.main()
