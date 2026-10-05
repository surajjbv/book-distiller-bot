"""The lease protocol (~/Code/botkit/PROTOCOL.md) with real processes against a fake `lms`, and config validation.
Run with  .venv/bin/python -m unittest discover tests"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from distiller.util import DistillerError, load_config

HERE = Path(__file__).parent
WORKER = """
import sys, time
from distiller.llm import LMStudio, ModelBusy
llm = LMStudio({"endpoint": "http://127.0.0.1:9/v1", "model": "qwen3.8-27b-mlx", "lms_path": sys.argv[1], "takeover_idle_minutes": 5})
time.sleep(float(sys.argv[2]))
try:
    llm.ready()
except ModelBusy:
    print("busy", flush=True); sys.exit(75)
print("acquired", flush=True)
time.sleep(float(sys.argv[3]))  # then exit: release() runs at exit
"""


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / "lms.json"
        self.env = {**os.environ, "FAKE_LMS_STATE": str(self.state), "LLM_LEASE_DIR": str(self.tmp / "leases"), "PYTHONWARNINGS": "ignore"}
        self.write({"models": [], "loads": 0, "unloads": 0})

    def write(self, s):
        self.state.write_text(json.dumps(s))

    def read(self):
        return json.loads(self.state.read_text())

    def worker(self, delay, hold):
        return subprocess.Popen([sys.executable, "-c", WORKER, str(HERE / "fake_lms.py"), str(delay), str(hold)],
                                env=self.env, cwd=HERE.parent, stdout=subprocess.PIPE, text=True)

    def leases(self):
        d = self.tmp / "leases"
        return [p.name for p in d.iterdir() if p.name != "lock"] if d.exists() else []

    def test_overlapping_and_killed_load_once_unload_once(self):
        a, b, k = self.worker(0, 1.5), self.worker(0.3, 2.5), self.worker(0.6, 60)
        self.assertEqual(k.stdout.readline().strip(), "acquired")
        k.kill()  # crashes holding a lease
        k.wait()
        c = self.worker(0, 1.5)
        for w in (a, b, c):
            self.assertEqual(w.stdout.readline().strip(), "acquired")
            w.wait()
        s = self.read()
        self.assertEqual((s["loads"], s["unloads"], s["models"]), (1, 1, []))
        self.assertEqual(self.leases(), [])

    def test_a_model_a_person_loaded_is_reused_and_left(self):
        self.write({"models": [{"type": "llm", "modelKey": "qwen3.8-27b-mlx", "identifier": "qwen3.8-27b-mlx", "contextLength": 42496,
                                "status": "idle", "lastUsedTime": time.time() * 1000}], "loads": 0, "unloads": 0})
        w = self.worker(0, 0.1)
        self.assertEqual(w.stdout.readline().strip(), "acquired")
        w.wait()
        s = self.read()
        self.assertEqual((s["loads"], s["unloads"], len(s["models"])), (0, 0, 1))

    def test_guardrail_refusal_exits_75(self):
        self.write({"models": [], "loads": 0, "unloads": 0, "fit": False})
        w = self.worker(0, 0.1)
        self.assertEqual(w.stdout.readline().strip(), "busy")
        self.assertEqual(w.wait(), 75)
        self.assertEqual(self.read()["loads"], 0)
        self.assertEqual(self.leases(), [])


class ConfigTests(unittest.TestCase):
    def test_validation_and_profile(self):
        p = Path(tempfile.mkdtemp()) / "config.json"
        p.write_text(json.dumps({"_comment": "x", "temperature": 0, "image_cap": 5}))
        cfg = load_config(p)
        self.assertEqual((cfg["temperature"], cfg["image_cap"], cfg["context_length"], cfg["parallel"]), (0, 5, 16384, 2))
        p.write_text(json.dumps({"image_cap": "5", "context_length": 4096, "notify": 1}))
        with self.assertRaises(DistillerError) as e:
            load_config(p)
        for part in ('"image_cap" must be int', 'unknown key "context_length"', '"notify" must be bool'):
            self.assertIn(part, str(e.exception))


if __name__ == "__main__":
    unittest.main()
