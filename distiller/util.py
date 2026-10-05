"""Shared helpers: config, paths, logging, token estimates, JSON IO, Drive, notifications."""
from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import re
import subprocess
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
INPUT_DIR = ROOT / "input"
WORK_DIR = ROOT / "work"
OUTPUT_DIR = ROOT / "output"
TEMPLATE_DIR = ROOT / "templates"
CONFIG_PATH = ROOT / "config.yaml"
STYLE_PATH = ROOT / "style.md"
BOOK_EXTS = {".pdf", ".epub"}

log = logging.getLogger("distiller")


class DistillerError(Exception):
    """An error with a user-facing explanation and fix."""


# ---------------------------------------------------------------- config

DEFAULTS = {
    "endpoint": "http://localhost:1234/v1",
    "lms_path": None,
    "model": "Qwen3.8-27B-MLX-4bit",
    "context_length": 16384,
    "parallel": 2,
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
    "drive_folder": None,
    "drive_subfolder": "Book Summaries",
    "watch_interval": 30,
    "notify": True,
}


def load_config(path: Path = CONFIG_PATH) -> dict:
    cfg = dict(DEFAULTS)
    if path.exists():
        cfg.update(yaml.safe_load(path.read_text()) or {})
    return cfg


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


# ---------------------------------------------------------------- Drive

def find_drive_folder(cfg: dict) -> Path:
    """Return the "<My Drive>/<Book Summaries>" folder, creating the subfolder if needed."""
    if cfg.get("drive_folder"):
        base = Path(os.path.expanduser(cfg["drive_folder"]))
        if not base.is_dir():
            raise DistillerError(f"drive_folder in config.yaml does not exist: {base}")
    else:
        hits = sorted(glob.glob(os.path.expanduser("~/Library/CloudStorage/GoogleDrive-*/My Drive")))
        if not hits:
            raise DistillerError(
                "Google Drive 'My Drive' folder not found under ~/Library/CloudStorage/.\n"
                "  Fix: open Google Drive for Desktop and sign in, or set drive_folder in config.yaml.")
        if len(hits) > 1:
            log.info("Several Google Drive accounts found; using %s (set drive_folder to override)", hits[0])
        base = Path(hits[0])
    target = base / cfg.get("drive_subfolder", "Book Summaries")
    target.mkdir(parents=True, exist_ok=True)
    return target


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
