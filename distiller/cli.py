"""Command line: ./run.sh [book] [--force] [--from STEP] | watch | preflight"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

from tqdm import tqdm

from .llm import LMStudio, ModelBusy
from .pipeline import STEPS, BookJob
from .util import (BOOK_EXTS, WORK_DIR, DistillerError, add_file_log, book_dirs,
                   load_config, log, notify, read_json, remove_log, sha256_file, slugify, write_json)

INDEX_PATH = WORK_DIR / "index.json"
LOCK_PATH = WORK_DIR / ".lock"
USAGE = """\
./run.sh                                  summarise every new PDF/EPUB in the input folder
./run.sh <book> [--force] [--from STEP]   one book (path, file name in the input folder, or part of a name)
./run.sh watch                            keep watching the input folder for new books
./run.sh preflight                        check LM Studio, the model and the folders
"""


class TqdmHandler(logging.Handler):
    def emit(self, record):
        tqdm.write(self.format(record), file=sys.stderr)


def input_books(inp: Path) -> list:
    return sorted(f.resolve() for f in inp.iterdir()
                  if f.is_file() and f.suffix.lower() in BOOK_EXTS and not f.name.startswith("."))


def find_book(arg: str, inp: Path) -> Path:
    for p in (Path(arg).expanduser(), inp / arg):
        if p.is_file():
            return p.resolve()
    hits = [f for f in input_books(inp) if arg.lower() in f.name.lower()]
    if len(hits) == 1:
        return hits[0]
    raise DistillerError(f"'{arg}' matches several books: {', '.join(h.name for h in hits)}" if hits
                         else f"Book not found: {arg} (looked for a path and in {inp})")


def preflight(cfg: dict, llm: LMStudio) -> tuple:
    llm.preflight()
    log.info("✓ LM Studio ready: %s", llm.model)
    inp, out = book_dirs(cfg)
    log.info("✓ Books from %s\n✓ Summaries to %s", inp, out)
    return inp, out


def process(path: Path, cfg: dict, llm: LMStudio, out: Path, force: bool = False, from_step: str | None = None) -> None:
    sha, index = sha256_file(path), read_json(INDEX_PATH, {})
    entry = index.get(sha)
    done = [out / Path(f).name for f in (entry or {}).get("files") or ([entry["output"]] if entry else [])]
    if done and not (force or from_step) and all(f.exists() for f in done):
        log.info("✓ %s already summarised → %s  (--force to redo)", path.name, done[0].name)
        return
    previous = (entry or {}).get("files") or ([entry["output"]] if entry else [])
    if entry:  # being redone: an interrupted --force/--from run resumes on a plain re-run
        write_json(INDEX_PATH, {k: v for k, v in index.items() if k != sha})
    workdir = WORK_DIR / slugify(path.stem)
    state = read_json(workdir / "state.json")
    if workdir.exists() and (force or (state and state.get("sha256") != sha)):
        shutil.rmtree(workdir)
    write_json(workdir / "state.json", {"sha256": sha, "source": str(path)})
    handler = add_file_log(workdir / "run.log")
    try:
        log.info("━━ %s", path.name)
        job = BookJob(path, cfg, llm, workdir)
        if from_step:
            job.reset_from(from_step)
        res = job.run()
        files = []
        for page in res["pages"]:
            f = out / page["name"]
            f.write_text(page["html"], encoding="utf-8")
            files.append(str(f))
        log.info("✓ Saved %s in %s", ", ".join(Path(f).name for f in files), out)
        for old in previous:  # an earlier run's files that this run no longer produces (e.g. 1 file -> 2 parts)
            stale = out / Path(old).name
            if stale.name not in {Path(f).name for f in files} and stale.exists():
                trash(stale)
        index = read_json(INDEX_PATH, {})
        index[sha] = {"source": path.name, "title": res["title"], "output": files[0], "files": files,
                      "model": llm.model, "finished": datetime.now().isoformat(timespec="seconds")}
        write_json(INDEX_PATH, index)
        if cfg.get("notify", True):
            parts = len(res["pages"])
            notify("Book Distiller", f"Summary ready: {res['title']} ({res['read_minutes']} min"
                                     + (f", {parts} parts)" if parts > 1 else ")"))
    except ModelBusy:
        raise
    except Exception as e:
        log.error("✗ %s failed: %s", path.name, e)
        log.debug("traceback", exc_info=True)
        if cfg.get("notify", True):
            notify("Book Distiller", f"Failed: {path.name}")
        raise
    finally:
        remove_log(handler)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    p = argparse.ArgumentParser(prog="./run.sh", usage=USAGE)
    p.add_argument("book", nargs="?", help="book to process, or 'watch' / 'preflight'")
    p.add_argument("--force", action="store_true", help="ignore previous work and redo everything")
    p.add_argument("--from", dest="from_step", choices=STEPS, help="redo from this step onwards")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    log.setLevel(logging.DEBUG)
    h = TqdmHandler()
    h.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    h.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(h)

    # Stop cleanly on Ctrl-C and on `kill` alike, even when started in the background (where SIGINT is ignored).
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    if args.book != "preflight":
        single_instance()
    try:
        cfg = load_config()
    except DistillerError as e:
        log.error("\n%s", e)
        return 1
    llm = LMStudio(cfg)
    try:
        inp, out = preflight(cfg, llm)
        if args.book == "preflight":
            return 0
        if args.book == "watch":
            return watch(cfg, llm, inp, out)
        books = [find_book(args.book, inp)] if args.book else input_books(inp)
        if not books:
            log.info("No PDF/EPUB in %s. Drop a book there and run ./run.sh again.", inp)
        failed = 0
        for b in books:
            try:
                process(b, cfg, llm, out, force=args.force, from_step=args.from_step)
            except ModelBusy:
                raise
            except DistillerError:
                failed += 1
        return 1 if failed else 0
    except ModelBusy as e:
        log.warning("%s: try again later (progress is saved)", e)
        return 75
    except DistillerError as e:
        log.error("\n%s", e)
        return 1
    except KeyboardInterrupt:
        log.error("\nInterrupted. Progress is saved; run the same command again to resume.")
        llm.release()
        LOCK_PATH.unlink(missing_ok=True)
        os._exit(130)  # don't wait for in-flight requests in worker threads; every saved file is already complete
    finally:
        llm.release()  # free the RAM when done (the last app out unloads the model)
        if LOCK_PATH.exists() and LOCK_PATH.read_text().strip() == str(os.getpid()):
            LOCK_PATH.unlink()


def trash(p: Path) -> None:
    """Move a superseded summary to the Trash (recoverable), not a permanent delete."""
    import subprocess
    subprocess.run(["osascript", "-e", f'tell application "Finder" to delete POSIX file "{p}"'], capture_output=True)
    log.info("  moved superseded %s to the Trash", p.name)


def single_instance() -> None:
    """Two runs would summarise the same books twice, so allow only one."""
    WORK_DIR.mkdir(exist_ok=True)
    try:
        pid = int(LOCK_PATH.read_text())
        os.kill(pid, 0)
        log.error("Another Book Distiller run is in progress (pid %d). Wait for it, or stop it first.", pid)
        sys.exit(1)
    except (FileNotFoundError, ValueError, ProcessLookupError):
        pass  # no lock, or a stale one from a crash
    LOCK_PATH.write_text(str(os.getpid()))


def watch(cfg: dict, llm: LMStudio, inp: Path, out: Path) -> int:
    log.info("Watching %s every %ss (Ctrl-C to stop)…", inp, cfg["watch_interval"])
    sizes, handled = {}, set()  # handled: (path, mtime) done/skipped/failed; retried only if the file changes
    while True:
        for b in input_books(inp):
            st = b.stat()
            stable = sizes.get(b) == st.st_size and time.time() - st.st_mtime > 5
            sizes[b] = st.st_size
            if stable and (b, st.st_mtime) not in handled:
                handled.add((b, st.st_mtime))
                try:
                    process(b, cfg, llm, out)
                except Exception:
                    pass  # logged; retried if the file changes
                llm.release()
        time.sleep(int(cfg["watch_interval"]))
