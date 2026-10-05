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

from .llm import LMStudio
from .pipeline import STEPS, BookJob
from .util import (BOOK_EXTS, INPUT_DIR, OUTPUT_DIR, WORK_DIR, DistillerError, add_file_log, find_drive_folder,
                   load_config, log, notify, read_json, remove_log, sha256_file, slugify, write_json)

INDEX_PATH = WORK_DIR / "index.json"
LOCK_PATH = WORK_DIR / ".lock"
USAGE = """\
./run.sh                                  summarise every new PDF/EPUB in input/
./run.sh <book> [--force] [--from STEP]   one book (path, file name in input/, or part of a name)
./run.sh watch                            keep watching input/ for new books
./run.sh preflight                        check LM Studio, the model and Google Drive
"""


class TqdmHandler(logging.Handler):
    def emit(self, record):
        tqdm.write(self.format(record), file=sys.stderr)


def input_books() -> list:
    return sorted(f.resolve() for f in INPUT_DIR.iterdir()
                  if f.is_file() and f.suffix.lower() in BOOK_EXTS and not f.name.startswith("."))


def find_book(arg: str) -> Path:
    for p in (Path(arg).expanduser(), INPUT_DIR / arg):
        if p.is_file():
            return p.resolve()
    hits = [f for f in input_books() if arg.lower() in f.name.lower()]
    if len(hits) == 1:
        return hits[0]
    raise DistillerError(f"'{arg}' matches several books: {', '.join(h.name for h in hits)}" if hits
                         else f"Book not found: {arg} (looked for a path and in input/)")


def preflight(cfg: dict, llm: LMStudio) -> Path:
    llm.preflight()
    log.info("✓ LM Studio ready: %s", llm.model)
    drive = find_drive_folder(cfg)
    log.info("✓ Google Drive: %s", drive)
    return drive


def process(path: Path, cfg: dict, llm: LMStudio, drive: Path, force: bool = False, from_step: str | None = None) -> None:
    sha, index = sha256_file(path), read_json(INDEX_PATH, {})
    entry = index.get(sha)
    if entry and not (force or from_step) and Path(entry["output"]).exists():
        log.info("✓ %s already summarised → %s  (--force to redo)", path.name, Path(entry["output"]).name)
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
        OUTPUT_DIR.mkdir(exist_ok=True)
        files = []
        for page in res["pages"]:
            out = OUTPUT_DIR / page["name"]
            out.write_text(page["html"], encoding="utf-8")
            shutil.copy2(out, drive / out.name)
            files.append(str(out))
        log.info("✓ Saved %s and copied to Google Drive", ", ".join(Path(f).name for f in files))
        for old in previous:  # an earlier run's files that this run no longer produces (e.g. 1 file -> 2 parts)
            for stale in (Path(old), drive / Path(old).name):
                if str(stale) not in files and stale.name not in {Path(f).name for f in files} and stale.exists():
                    trash(stale)
        index = read_json(INDEX_PATH, {})
        index[sha] = {"source": path.name, "title": res["title"], "output": files[0], "files": files,
                      "model": llm.model, "finished": datetime.now().isoformat(timespec="seconds")}
        write_json(INDEX_PATH, index)
        if cfg.get("notify", True):
            parts = len(res["pages"])
            notify("Book Distiller", f"Summary ready: {res['title']} ({res['read_minutes']} min"
                                     + (f", {parts} parts)" if parts > 1 else ")"))
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
    cfg = load_config()
    llm = LMStudio(cfg)
    try:
        drive = preflight(cfg, llm)
        if args.book == "preflight":
            return 0
        if args.book == "watch":
            return watch(cfg, llm, drive)
        books = [find_book(args.book)] if args.book else input_books()
        if not books:
            log.info("No PDF/EPUB in %s. Drop a book there and run ./run.sh again.", INPUT_DIR)
        failed = 0
        for b in books:
            try:
                process(b, cfg, llm, drive, force=args.force, from_step=args.from_step)
            except DistillerError:
                failed += 1
        return 1 if failed else 0
    except DistillerError as e:
        log.error("\n%s", e)
        return 1
    except KeyboardInterrupt:
        log.error("\nInterrupted. Progress is saved; run the same command again to resume.")
        llm.unload()
        LOCK_PATH.unlink(missing_ok=True)
        os._exit(130)  # don't wait for in-flight requests in worker threads; every saved file is already complete
    finally:
        llm.unload()  # free the RAM when done
        if LOCK_PATH.exists() and LOCK_PATH.read_text().strip() == str(os.getpid()):
            LOCK_PATH.unlink()


def trash(p: Path) -> None:
    """Move a superseded summary to the Trash (recoverable), not a permanent delete."""
    import subprocess
    subprocess.run(["osascript", "-e", f'tell application "Finder" to delete POSIX file "{p}"'], capture_output=True)
    log.info("  moved superseded %s to the Trash", p.name)


def single_instance() -> None:
    """Two runs would fight over the model (one finishing unloads the other's), so allow only one."""
    WORK_DIR.mkdir(exist_ok=True)
    try:
        pid = int(LOCK_PATH.read_text())
        os.kill(pid, 0)
        log.error("Another Book Distiller run is in progress (pid %d). Wait for it, or stop it first.", pid)
        sys.exit(1)
    except (FileNotFoundError, ValueError, ProcessLookupError):
        pass  # no lock, or a stale one from a crash
    LOCK_PATH.write_text(str(os.getpid()))


def watch(cfg: dict, llm: LMStudio, drive: Path) -> int:
    log.info("Watching %s every %ss (Ctrl-C to stop)…", INPUT_DIR, cfg["watch_interval"])
    sizes, handled = {}, set()  # handled: (path, mtime) done/skipped/failed; retried only if the file changes
    while True:
        for b in input_books():
            st = b.stat()
            stable = sizes.get(b) == st.st_size and time.time() - st.st_mtime > 5
            sizes[b] = st.st_size
            if stable and (b, st.st_mtime) not in handled:
                handled.add((b, st.st_mtime))
                try:
                    process(b, cfg, llm, drive)
                except Exception:
                    pass  # logged; retried if the file changes
                llm.unload()
        time.sleep(int(cfg["watch_interval"]))
