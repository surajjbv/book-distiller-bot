"""LM Studio client: loads the one configured model via `lms`, runs JSON-schema chat completions."""
from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import jsonschema
import requests

from .util import DistillerError, est_tokens, log

ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
LEADING_JUNK = re.compile(r"^(?:[\s,;:•·]+|[-–—]\s+)+")


def strip_think(s: str) -> str:
    s = re.sub(r"<think>.*?</think>", "", s or "", flags=re.S | re.I)
    if "</think>" in s:  # template emitted only the closing tag
        s = s.rsplit("</think>", 1)[1]
    return s.split("<think>", 1)[0].strip()  # drop unterminated reasoning


def parse_json_loose(s: str):
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", strip_think(s))
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        a, b = s.find("{"), s.rfind("}")
        if 0 <= a < b:
            return json.loads(s[a:b + 1])
        raise


def tidy(obj):
    """Trim whitespace and stray leading punctuation/bullets from every string."""
    if isinstance(obj, str):
        return LEADING_JUNK.sub("", obj).strip()
    if isinstance(obj, list):
        return [tidy(x) for x in obj]
    if isinstance(obj, dict):
        return {k: tidy(v) for k, v in obj.items()}
    return obj


# The model and the lease protocol shared with the Node bots (the same as kit.js in the Node bots).
# Every app loads the same profile, so whoever needs the model reuses what another one loaded.
PROFILE = {"identifier": "qwen3.8-27b-mlx", "context": 16384, "parallel": 2, "ttl": 600}
LEASE_DIR = Path(os.environ.get("LLM_LEASE_DIR") or "~/.local/state/llm-lease").expanduser()
LOCK, OWNED = LEASE_DIR / "lock", LEASE_DIR / "owned"


class ModelBusy(DistillerError):
    """The model can't be had now (busy, or too little memory): exit 75, try again later."""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


@contextmanager
def _locked():
    """Hold the protocol's lock dir (atomic mkdir; one left by a crash is stale after 120 s)."""
    LEASE_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            LOCK.mkdir()
            break
        except FileExistsError:
            try:
                if time.time() - LOCK.stat().st_mtime > 120:
                    LOCK.rmdir()
            except OSError:
                pass  # just released
            time.sleep(0.1)
    try:
        yield
    finally:
        shutil.rmtree(LOCK, ignore_errors=True)


def _leases() -> list:
    return [p for p in LEASE_DIR.iterdir() if re.fullmatch(r".+\.\d+", p.name)]


def _drop_dead() -> None:
    for p in _leases():
        if not _alive(int(p.name.rsplit(".", 1)[1])):
            p.unlink(missing_ok=True)


class LMStudio:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.base = cfg["endpoint"].rstrip("/")
        self.model = cfg["model"]
        self.ctx = PROFILE["context"]
        self.lms = self._find_lms()
        self.identifier = None  # set while we hold a lease and the model is loaded
        self.lease = LEASE_DIR / f"book-distiller-bot.{os.getpid()}"
        self._lock = threading.Lock()
        self.calls = self.valid_first = self.tokens = 0
        self.seconds = 0.0
        atexit.register(self.release)

    # ------------------------------------------------------------ model management

    def _find_lms(self) -> str | None:
        p = os.path.expanduser(self.cfg.get("lms_path") or "~/.lmstudio/bin/lms")
        return p if os.path.exists(p) else shutil.which("lms")

    def _run(self, *args: str, timeout: int = 600) -> str:
        if not self.lms:
            raise DistillerError("LM Studio CLI `lms` not found.\n  Fix: open LM Studio once, or set lms_path in config.json.")
        r = subprocess.run([self.lms, *args], capture_output=True, text=True, timeout=timeout)
        return ANSI.sub("", (r.stdout or "") + (r.stderr or ""))

    def server_up(self) -> bool:
        try:
            return requests.get(f"{self.base}/models", timeout=3).ok
        except requests.RequestException:
            return False

    def preflight(self) -> None:
        """Server reachable (started if needed; never stopped, others use it), model installed, fits the guardrail."""
        if not self.server_up():
            log.info("Starting the LM Studio server…")
            self._run("server", "start", timeout=60)
            for _ in range(20):
                if self.server_up():
                    break
                time.sleep(1)
            else:
                raise DistillerError(f"LM Studio server not reachable at {self.base}.\n"
                                     "  Fix: open LM Studio (Bionic.app) or run `lms server start`.")
        try:
            keys = {m["modelKey"] for m in json.loads(self._run("ls", "--json", timeout=60))}
        except (json.JSONDecodeError, KeyError):
            keys = set()
        if self.model not in keys:
            raise DistillerError(f"Model '{self.model}' is not downloaded in LM Studio.\n"
                                 "  Fix: lms get https://huggingface.co/lmstudio-community/Qwen3.8-27B-MLX-4bit")
        if not self._ps():  # with a model loaded the estimate is meaningless; the lease protocol decides then
            out = self._estimate()
            if "will fail" in out:
                mem = re.search(r"Estimated Total Memory:\s*([\d.]+\s*\w+)", out)
                raise DistillerError(
                    f"Not enough free memory for {self.model} (LM Studio estimates {mem.group(1) if mem else '?'}).\n"
                    "  Fix: quit other apps (Chrome is usually the biggest) and run again. The guardrail stays on.")

    def _estimate(self) -> str:
        return self._run("load", self.model, "--estimate-only", "--context-length", str(PROFILE["context"]),
                         "--parallel", str(PROFILE["parallel"]), "-y", timeout=120)

    def _ps(self) -> list:
        try:
            return json.loads(self._run("ps", "--json", timeout=60) or "[]")
        except json.JSONDecodeError:
            return []

    def _try_acquire(self):
        """One attempt under the lock: our identifier, or ('busy', names) / ('refused', why)."""
        with _locked():
            _drop_dead()
            self.lease.write_text(time.strftime("%Y-%m-%dT%H:%M:%S\n"))
            ps = self._ps()
            ours = [m for m in ps if self.model in (m.get("modelKey"), m.get("path"), m.get("indexedModelIdentifier"))
                    and (m.get("contextLength") or 0) >= PROFILE["context"]]
            if ours:
                return ours[0]["identifier"]
            OWNED.unlink(missing_ok=True)  # our earlier load is gone (TTL), so the marker is stale
            busy, idle_min = [], float(self.cfg.get("takeover_idle_minutes", 5))
            for m in [m for m in ps if m.get("type") != "embedding"]:
                last = m.get("lastUsedTime")
                idle = (time.time() - last / 1000) / 60 if m.get("status") == "idle" and last else 0
                if idle < idle_min:
                    busy.append(m.get("identifier", "?"))
                    continue
                log.info("Unloading %s: idle for %d min", m["identifier"], idle)
                self._run("unload", m["identifier"], timeout=120)
            if busy:
                return ("busy", busy)
            if "will fail" in self._estimate():
                return ("refused", f"LM Studio's memory guardrail would refuse {self.model} now")
            log.info("Loading %s…", self.model)
            out = self._run("load", self.model, "--identifier", PROFILE["identifier"], "--context-length",
                            str(PROFILE["context"]), "--parallel", str(PROFILE["parallel"]), "--ttl", str(PROFILE["ttl"]),
                            "-y", timeout=900)
            if not any(m.get("identifier") == PROFILE["identifier"] for m in self._ps()):
                return ("refused", f"LM Studio could not load {self.model}: {out.strip()[-300:]}")
            OWNED.write_text(f"{self.lease.name} {time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
            return PROFILE["identifier"]

    def ready(self) -> None:
        """Hold a lease and have the model loaded (cheap once held). Waits up to 10 min while another app's model is
        busy; if the model still can't be had, raises ModelBusy (exit 75)."""
        with self._lock:
            if self.identifier:
                return
            deadline, waited = time.time() + 600, 0
            while True:
                r = self._try_acquire()
                if isinstance(r, str):
                    self.identifier = r
                    return
                if r[0] == "refused" or time.time() > deadline:
                    self.release()
                    raise ModelBusy(r[1] if r[0] == "refused" else f"LM Studio stayed busy with {', '.join(r[1])} for 10 min")
                if waited % 120 == 0:
                    log.info("Waiting for %s to finish in LM Studio (another app is using it)…", ", ".join(r[1]))
                time.sleep(15)
                waited += 15

    def release(self) -> None:
        """Drop our lease; the last app out unloads the model if an app (not a person) loaded it. Safe to repeat."""
        self.identifier = None
        if not self.lease.exists():
            return
        try:
            with _locked():
                self.lease.unlink(missing_ok=True)
                _drop_dead()
                if not _leases() and OWNED.exists():
                    self._run("unload", PROFILE["identifier"], timeout=120)  # an error means it is already gone
                    OWNED.unlink(missing_ok=True)
                    log.info("Model unloaded")
        except Exception as e:  # never let cleanup hide the real error
            log.warning("Model release failed: %s", e)


    # ------------------------------------------------------------ chat

    def chat_json(self, messages: list, schema: dict, name: str, max_tokens: int | None = None) -> dict:
        """One chat completion constrained to `schema` (thread-safe). Returns the validated object."""
        prompt_est = sum(est_tokens(m["content"]) if isinstance(m["content"], str) else 1500 for m in messages)
        max_tok = max(512, min(int(max_tokens or self.cfg["max_output_tokens"]), self.ctx - prompt_est - 256))
        err = None
        with self._lock:
            self.calls += 1
        for attempt in range(4):
            if attempt:
                time.sleep(15 * attempt)  # LM Studio busy or reloading: back off instead of failing at once
            self.ready()
            t0 = time.time()
            try:
                content, finish, usage = self._stream(messages, schema, name, max_tok)
            except requests.RequestException as e:
                err = str(e)[:400]
                self.identifier = None  # e.g. the model was unloaded under us: take it again before the next try
                log.warning("[%s] request failed: %s (attempt %d)", name, err, attempt + 1)
                continue
            with self._lock:
                self.tokens += usage.get("completion_tokens", 0)
                self.seconds += time.time() - t0
            try:
                obj = parse_json_loose(content)
                jsonschema.validate(obj, schema)
            except (json.JSONDecodeError, jsonschema.ValidationError) as e:
                err = f"invalid JSON ({finish}): {str(e)[:200]}"
                log.warning("[%s] %s (attempt %d)", name, err, attempt + 1)
                if finish == "length":
                    max_tok = min(int(max_tok * 1.5), self.ctx - prompt_est - 256)
                continue
            with self._lock:
                self.valid_first += attempt == 0
            log.debug("[%s] %.0fs, %s tokens", name, time.time() - t0, usage.get("completion_tokens"))
            return tidy(obj)
        raise DistillerError(f"[{name}] no valid answer after 4 attempts: {err}")

    def _stream(self, messages: list, schema: dict, name: str, max_tok: int) -> tuple:
        """Streamed completion (bytes keep flowing, so long generations never hit an idle-connection drop).
        Returns (content, finish_reason, usage).

        Text calls use the raw completions endpoint with Qwen's chat format ending in an empty <think> block:
        that is the only reliable way to switch Qwen3.8's thinking off (via the chat endpoint it writes its
        reasoning into the JSON). Image calls must use the chat endpoint."""
        body = {"model": self.identifier or PROFILE["identifier"], "temperature": self.cfg["temperature"], "max_tokens": max_tok, "stream": True,
                "stream_options": {"include_usage": True},
                "response_format": {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}}}
        if any(isinstance(m["content"], list) for m in messages):
            url, body["messages"] = f"{self.base}/chat/completions", messages
        else:
            url = f"{self.base}/completions"
            body["prompt"] = "".join(f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>\n" for m in messages) \
                + "<|im_start|>assistant\n<think>\n\n</think>\n\n"
            body["stop"] = ["<|im_end|>"]
        with requests.post(url, json=body, stream=True, timeout=(10, int(self.cfg["request_timeout"]))) as r:
            r.encoding = "utf-8"  # SSE responses carry no charset; requests would otherwise decode as Latin-1
            if not r.ok:
                raise requests.HTTPError(f"HTTP {r.status_code}: {r.text[:300]}")
            text, reasoning, finish, usage = [], [], None, {}
            for line in r.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                ev = json.loads(data)
                if ev.get("error"):
                    raise requests.HTTPError(f"LM Studio: {str(ev['error'])[:300]}")
                usage = ev.get("usage") or usage
                for ch in ev.get("choices", []):
                    delta = ch.get("delta") or {}
                    text.append(ch.get("text") or delta.get("content") or "")
                    reasoning.append(delta.get("reasoning_content") or delta.get("reasoning") or "")
                    finish = ch.get("finish_reason") or finish
        return "".join(text) or "".join(reasoning), finish, usage

    def stats(self) -> str:
        tps = self.tokens / self.seconds if self.seconds else 0
        return f"{self.calls} calls, {self.valid_first}/{self.calls} valid first try, {self.tokens:,} tokens, {tps:.1f} tok/s per request"
