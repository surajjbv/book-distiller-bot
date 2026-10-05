"""Shared helpers: config, folders, logging, token estimates, JSON IO, notifications."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = ROOT / "work"
TEMPLATE_DIR = ROOT / "templates"
CONFIG_PATH = ROOT / "config.json"
STYLE_PATH = ROOT / "style.md"
BOOK_EXTS = {".pdf", ".epub"}

log = logging.getLogger("distiller")


class DistillerError(Exception):
    """An error with a user-facing explanation and fix."""


# ---------------------------------------------------------------- config

DEFAULTS = {
    "endpoint": "http://localhost:1234/v1",
    "lms_path": None,
    "model": "qwen3.8-27b-mlx",  # LM Studio key; the load profile (context, parallel, TTL) is fixed in llm.PROFILE
    "max_minutes": 120,
    "takeover_idle_minutes": 5,
    "chunk_tokens": 8000,
    "min_chunk_tokens": 700,
    "temperature": 0.3,
    "max_output_tokens": 3500,
    "request_timeout": 1800,
    "image_cap": 15,
    "image_candidates": 30,
    "min_image_px": 160,
    "quote_match_threshold": 90,
    "input_dir": "input",  # books to summarise (PDF, EPUB); BOOKS_INPUT_DIR in .env overrides
    "output_dir": "output",  # finished summaries (a book whose summary is here is skipped); BOOKS_OUTPUT_DIR in .env
    "watch_interval": 30,
    "notify": True,
}


def load_config(path: Path = CONFIG_PATH) -> dict:
    """config.json over DEFAULTS, validated (unknown keys and wrong types are errors); "_comment" keys are ignored.
    context_length and parallel come from the shared load profile, not from the file."""
    from .llm import PROFILE
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise DistillerError(f"{path.name}: {e}")
    problems = []
    for k, v in data.items():
        if k.startswith("_"):
            continue
        d = DEFAULTS.get(k, ...)
        if d is ...:
            problems.append(f'unknown key "{k}"')
        elif not (d is None or v is None or type(v) is type(d) or (type(d) is float and type(v) is int)):
            problems.append(f'"{k}" must be {type(d).__name__}, not {type(v).__name__}')
    if problems:
        raise DistillerError(f"{path.name}: " + "; ".join(problems))
    cfg = {**DEFAULTS, **{k: v for k, v in data.items() if not k.startswith("_")}}
    cfg.update(context_length=PROFILE["context"], parallel=PROFILE["parallel"])
    # Your own folders are personal, so they live in .env (gitignored), not in config.json.
    env = read_env(path.parent / ".env")
    cfg.update({k: env[e] for k, e in (("input_dir", "BOOKS_INPUT_DIR"), ("output_dir", "BOOKS_OUTPUT_DIR")) if env.get(e)})
    return cfg


def read_env(path: Path) -> dict:
    """KEY=value lines of a .env file (no file: empty)."""
    if not path.exists():
        return {}
    pairs = (line.split("=", 1) for line in path.read_text().splitlines() if re.match(r"\s*[A-Z][A-Z0-9_]*\s*=", line))
    return {k.strip(): v.strip().strip('"').strip("'") for k, v in pairs}


def read_style() -> str:
    return STYLE_PATH.read_text().strip() if STYLE_PATH.exists() else ""


# ---------------------------------------------------------------- logging

def add_file_log(path: Path) -> logging.Handler:
    path.parent.mkdir(parents=True, exist_ok=True)
    h = logging.FileHandler(path, encoding="utf-8")
    h.setLevel(logging.DEBUG)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    return h


def remove_log(h: logging.Handler) -> None:
    log.removeHandler(h)
    h.close()


# ---------------------------------------------------------------- concurrency

def pmap(fn, items, workers: int) -> list:
    """Parallel map in input order. On the first error, queued items are cancelled instead of run."""
    ex = ThreadPoolExecutor(max(1, workers))
    futures = [ex.submit(fn, x) for x in items]
    try:
        return [f.result() for f in futures]
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


# ---------------------------------------------------------------- text helpers

def est_tokens(text: str) -> int:
    """Conservative token estimate (~3.6 chars/token for English prose)."""
    return int(len(text) / 3.6) + 1


def slugify(s: str, maxlen: int = 60) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s[:maxlen].strip("-") or "book"


def safe_filename(s: str) -> str:
    s = re.sub(r'[/\\:*?"<>|\n\r\t]+', " ", s)
    return re.sub(r"\s+", " ", s).strip()[:150] or "Book"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False))
    os.replace(tmp, path)  # atomic: a crash never leaves a half-written file


# ---------------------------------------------------------------- folders

def book_dirs(cfg: dict) -> tuple:
    """(input, output) folders from config.json (relative to this project unless absolute; ~ allowed), created."""
    dirs = tuple((ROOT / os.path.expanduser(cfg[k])).resolve() for k in ("input_dir", "output_dir"))
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
    return dirs


# ---------------------------------------------------------------- notifications

def notify(title: str, message: str) -> None:
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')
    try:
        subprocess.run(["osascript", "-e",
                        f'display notification "{esc(message)}" with title "{esc(title)}" sound name "Glass"'],
                       check=False, capture_output=True, timeout=10)
    except Exception:  # notifications are best-effort
        pass
