"""LM Studio client: loads the one configured model via `lms`, runs JSON-schema chat completions."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time

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


class LMStudio:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.base = cfg["endpoint"].rstrip("/")
        self.model = cfg["model"]
        self.ctx = int(cfg["context_length"])
        self.lms = self._find_lms()
        self.loaded = False
        self._lock = threading.Lock()
        self.calls = self.valid_first = self.tokens = 0
        self.seconds = 0.0

    # ------------------------------------------------------------ model management

    def _find_lms(self) -> str | None:
        p = os.path.expanduser(self.cfg.get("lms_path") or "~/.lmstudio/bin/lms")
        return p if os.path.exists(p) else shutil.which("lms")

    def _run(self, *args: str, timeout: int = 600) -> str:
        if not self.lms:
            raise DistillerError("LM Studio CLI `lms` not found.\n  Fix: open LM Studio once, or set lms_path in config.yaml.")
        r = subprocess.run([self.lms, *args], capture_output=True, text=True, timeout=timeout)
        return ANSI.sub("", (r.stdout or "") + (r.stderr or ""))

    def server_up(self) -> bool:
        try:
            return requests.get(f"{self.base}/models", timeout=3).ok
        except requests.RequestException:
            return False

    def preflight(self) -> None:
        """Server reachable (started if needed), model installed, and it fits under LM Studio's guardrail."""
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
        if not self._ps():  # with another app's model loaded the estimate is meaningless; ready() will wait instead
            out = self._run("load", self.model, "--context-length", str(self.ctx), "--estimate-only", "-y", timeout=120)
            if "will fail" in out:
                mem = re.search(r"Estimated Total Memory:\s*([\d.]+\s*\w+)", out)
                raise DistillerError(
                    f"Not enough free memory for {self.model} (LM Studio estimates {mem.group(1) if mem else '?'}).\n"
                    "  Fix: quit other apps (Chrome is usually the biggest) and run again. The guardrail stays on.")

    def _ps(self) -> list:
        try:
            return json.loads(self._run("ps", "--json", timeout=60) or "[]")
        except json.JSONDecodeError:
            return []

    def _loaded_id(self) -> str | None:
        return next((m["identifier"] for m in self._ps() if self.model in (m.get("modelKey"), m.get("identifier"))), None)

    def ready(self) -> None:
        """Make sure our model, and only our model, is loaded before a request.

        LM Studio is shared (scheduled bots, other apps). We never unload someone else's model: if another model
        is busy we unload ours (if needed) and wait for theirs to finish, so two big models never sit in RAM
        together; one left idle for `takeover_idle_minutes` is treated as abandoned and unloaded. Checking before every request also stops LM Studio from auto-loading ours next to theirs."""
        with self._lock:
            waited = 0
            while True:
                ps = self._ps()
                ours = [m for m in ps if self.model in (m.get("modelKey"), m.get("identifier"))]
                others = [m for m in ps if m not in ours and m.get("type") != "embedding"]
                idle_s = 60 * float(self.cfg.get("takeover_idle_minutes", 5))
                for m in [m for m in others if m.get("status") == "idle"
                          and time.time() - (m.get("lastUsedTime") or time.time() * 1000) / 1000 >= idle_s]:
                    log.info("Unloading %s: another app left it idle for %d+ min", m["identifier"], idle_s // 60)
                    self._run("unload", m["identifier"], timeout=120)
                    others.remove(m)
                if not others:
                    break
                if ours:
                    self._run("unload", self.model, timeout=120)
                if waited % 120 == 0:
                    log.info("Waiting for %s to finish in LM Studio (another app is using it)…",
                             ", ".join(m.get("identifier", "?") for m in others))
                time.sleep(15)
                waited += 15
            if ours and not others:
                self.loaded = True
                return
            log.info("Loading %s…", self.model)
            out = self._run("load", self.model, "--context-length", str(self.ctx), "--parallel",
                            str(self.cfg.get("parallel", 2)), "--identifier", self.model, "-y", timeout=900)
            if self._loaded_id() is None:
                raise DistillerError(f"Failed to load {self.model}:\n{out.strip()[-500:]}")
            self.loaded = True

    def unload(self) -> None:
        if self.loaded and self._loaded_id():
            self._run("unload", self.model, timeout=120)
        self.loaded = False

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
        body = {"model": self.model, "temperature": self.cfg["temperature"], "max_tokens": max_tok, "stream": True,
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
