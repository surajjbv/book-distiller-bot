# Book Distiller

Drop a PDF or EPUB into `input/`. Book Distiller splits it into chapters, has **Qwen3.8‑27B** (running locally in
LM Studio) take structured notes on each one, checks every quote against the book, writes and audits a book-level
summary, and renders one HTML file to `output/` and to **Google Drive → My Drive → Book Summaries**.
Nothing leaves your Mac except through your own Drive sync.

```
input/book.pdf → chunk → plan → extract (2 at a time) → verify quotes → synthesize + audit → figures → render (parts)
                  │        work/<book>/notes/NN.json                       work/<book>/synthesis/      output/<Title> – Summary.html
                  └ TOC / nav → chapter headings → fixed 8K-token chunks                                + Drive/Book Summaries/
```

## Setup

Needs macOS, Python 3.9+, LM Studio (Bionic.app) with its `lms` CLI, Google Drive for Desktop, and the model:

```bash
lms get https://huggingface.co/lmstudio-community/Qwen3.8-27B-MLX-4bit
cd ~/Code/book-distiller
./run.sh preflight      # first run creates .venv; checks LM Studio, the model, memory and Drive
```

The bot starts the LM Studio server if it is off, loads Qwen3.8 itself (`--context-length 16384 --parallel 2`)
and unloads it when done. LM Studio's model loading guardrails stay on.

**Sharing LM Studio.** Other apps use the same LM Studio (the school reminder and PGRS bots load Gemma at their
scheduled times). The distiller never unloads a model another app is using: if one is busy it unloads its own and
waits ("Waiting for … to finish") until the other app is done, then reloads Qwen and carries on. A model another
app left loaded and idle for `takeover_idle_minutes` (default 5) is treated as abandoned and unloaded. Two big
models are never in memory together. A bot that starts while a book is running waits or retries on its own.

## Usage

| Command | What it does |
|---|---|
| `./run.sh` | Summarise every new PDF/EPUB in `input/`. Finished books (by file hash) are skipped. |
| `./run.sh <book>` | One book: a path, a file name in `input/`, or part of a name (`./run.sh thinketh`). |
| `./run.sh <book> --force` | Throw away previous work for that book and redo everything. |
| `./run.sh <book> --from synthesize` | Keep the chapter notes and redo the rest. Steps: `chunk`, `plan`, `extract`, `synthesize`, `images`, `render`. |
| `./run.sh watch` | Keep running and pick up new books as they land in `input/`. |
| `./run.sh preflight` | Check LM Studio, the model, memory and Drive. |

Add `-v` for debug output. Every book logs to `work/<book>/run.log`. Each chapter's notes and each synthesis step
are saved as soon as they finish, so after a crash, Ctrl‑C or `kill` (both stop it within a second, unloading the
model) the same command resumes where it stopped. Only one run at a time: a second one exits with a message.
You get a macOS notification when a book is done.

**Speed** (Qwen3.8‑27B 4-bit MLX on a Mac mini M6, 32 GB): it reads ~106 tokens/s and writes ~8–9 tokens/s per
request. Two requests run at once (`parallel: 2`; 4 is faster but makes a 32 GB Mac swap); every synthesis and
audit call shares one prompt prefix so LM Studio reads the notes once; and the audit returns only corrections.

## Tuning the style

`style.md` is injected into every writing prompt (chapter notes, synthesis, audit). Edit the reader, tone, depth,
length and formatting there; its "20–30 minute read" line sets the size of one part. Then:

```bash
./run.sh <book> --from synthesize   # new synthesis in the new style (reuses chapter notes)
./run.sh <book> --from extract      # redo the chapter notes too
```

Only `**bold**` and `*italic*` are rendered; the LLM never writes HTML (`templates/summary.html.j2` does the layout).

## How it works (and where to change it)

Every step saves its result in `work/<book>/` before the next one starts, so a run can stop and resume anywhere,
and `--from STEP` redoes one step and everything after it.

| # | Step | What happens | Code | Change it with |
|---|---|---|---|---|
| 1 | **Detect** | Finds new `.pdf`/`.epub` files in `input/`, fingerprints each (SHA‑256) and skips books already in `work/index.json`. | `cli.py` → `process()` | `--force` to redo a book |
| 2 | **Read** | Turns the book into one text with a page map, chapter headings and image positions. PDF: PyMuPDF, running headers and page numbers removed. EPUB: ebooklib, in reading order. A scanned PDF stops with an `ocrmypdf` command. | `ingest.py` → `load_pdf()`, `load_epub()` | — |
| 3 | **Chunk** | Splits into chapters: PDF table of contents / EPUB nav first, then "Chapter N" headings, then big-font headings, else fixed ~8K-token pieces. Skips copyright, contents, acknowledgements, notes, index, bibliography and Gutenberg boilerplate; merges tiny chapters and splits huge ones at paragraph breaks. | `chunk.py` → `build_chunks()`; skip lists `SKIP_EXACT`, `SKIP_PREFIX` | `chunk_tokens`, `min_chunk_tokens` |
| 4 | **Plan** | The model reads the contents and opening and decides how many minutes of summary the book deserves (20 min–2 h). That is split into 30-minute **parts** (max 4) and sets each chapter's word budget. Saved in `plan.json`. | `plan.py` → `plan_book()`, `budgets()` | `max_minutes`; the "20–30 minute read" line in `style.md` sets the part size; edit `plan.json` + `--from extract` to force a length |
| 5 | **Extract** | Two chapters at a time, the model writes structured notes: core idea, summary, key arguments, frameworks, data, examples, quotes with pages, takeaways. List sizes scale with the chapter's budget and are enforced by LM Studio. Saved per chapter in `notes/NN.json`. | `extract.py` → `extraction_messages()` (prompt), `note_schema()` (fields and limits) | `style.md`, `parallel` |
| 6 | **Verify** | Every quote is fuzzy-matched against the book. Matches get the book's exact wording and true page; the rest are dropped. Report in `verify.json`. | `extract.py` → `verify_quotes()` | `quote_match_threshold` |
| 7 | **Synthesize** | Whole-book overview in three calls: (a) TL;DR, core thesis, themes; then in parallel (b) frameworks, key stories and (c) takeaways, critique, cheat sheet. Notes too big for the context are condensed in groups first. | `synth.py` → `PARTS` (prompts and schemas), `Synthesizer.run()` | `style.md`; `SYNTH_MAX` in `plan.py` |
| 8 | **Audit** | One call checks the overview against the notes and returns only fixes (replace / remove / add) for unsupported claims, contradictions, gaps or wrong chapter refs. Fixes are validated, applied, and listed at the bottom of Part 1. | `synth.py` → audit prompt in `run()`, `apply_fixes()` | — |
| 9 | **Figures** | Pulls images and vector charts, drops small, odd-shaped, repeated and duplicate ones; the model keeps only diagrams, charts and tables and captions them. | `images.py` → `candidates()`, `process_images()` | `image_cap`, `image_candidates`, `min_image_px` |
| 10 | **Render** | Splits chapters into the planned parts (book order, similar length; Part 1 also holds the overview) and writes one HTML file per part in the RTI-dashboard style, with links between parts. The model never writes HTML. | `pipeline.py` → `render()`; `plan.py` → `split_parts()`; `templates/summary.html.j2` | the template |
| 11 | **Save** | Writes `output/<Title> – Summary.html` (or `… (Part 1 of 3).html` …), copies to Drive → Book Summaries, moves superseded files from an earlier run to the Trash, sends a notification. | `cli.py` → `process()` | `drive_folder`, `drive_subfolder`, `notify` |

**Model calls** go through `llm.py`: it makes sure Qwen3.8 (and only Qwen3.8) is loaded, waits for other apps
using LM Studio, streams every response, and retries with a pause. Text calls use Qwen's own prompt format with
thinking switched off; figure checks use the chat endpoint because they send images.

## Files

```
config.yaml   model, endpoint, context, parallel, chunk sizes, temperature, image cap, Drive override
style.md      the voice of your summaries
input/        drop books here                 output/   finished summaries
work/<book>/  doc.json, chunks.json, plan.json, notes/, synthesis/, images/, verify.json, run.log
distiller/    ingest, chunk, plan, extract, synth, images, render, pipeline, cli, llm, util
templates/    summary.html.j2
tests/        .venv/bin/python -m unittest discover tests   (offline, no model needed)
```

`drive_folder` in `config.yaml` overrides Drive auto-detection (`~/Library/CloudStorage/GoogleDrive-*/My Drive`).

## Troubleshooting

- **Not enough free memory for qwen3.8-27b-mlx**: quit other apps (Chrome is usually the biggest) and run again.
  Don't relax the guardrail.
- **"Waiting for … to finish"**: another app has a model loaded; the run continues by itself once it's unloaded.
- **No usable text layer**: the PDF is scanned. `brew install ocrmypdf`, then `ocrmypdf --skip-text in.pdf out.pdf`.
- **Odd chapter splits**: check the chunk list at the top of `work/<book>/run.log`, tune `chunk_tokens` /
  `min_chunk_tokens`, then `--from chunk`.
