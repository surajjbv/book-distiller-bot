"""The lease protocol (as in the Node bots' kit.js) with real processes against a fake `lms` (written to a temp
folder below), and config validation. Run with  .venv/bin/python -m unittest discover tests"""
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
FAKE_LMS = '''#!/usr/bin/env python3
"""Stand-in for LM Studio's `lms` CLI for the lease tests; state (models, loads, unloads, fit) in $FAKE_LMS_STATE."""
import json
import os
import sys
import time

path = os.environ["FAKE_LMS_STATE"]
s = json.load(open(path))
a = sys.argv[1:]
opt = lambda k: a[a.index(k) + 1]
if a[0] == "ps":
    print(json.dumps(s["models"]))
elif a[0] == "load" and "--estimate-only" in a:
    print("This model will fail to load" if s.get("fit") is False else "This model may be loaded")
elif a[0] == "load":
    time.sleep(0.3)
    s["models"].append({"type": "llm", "modelKey": a[1], "identifier": opt("--identifier"), "contextLength": int(opt("--context-length")),
                        "status": "idle", "lastUsedTime": time.time() * 1000})
    s["loads"] = s.get("loads", 0) + 1
elif a[0] == "unload":
    if not any(m["identifier"] == a[1] for m in s["models"]):
        print(f"not loaded: {a[1]}")
        sys.exit(1)
    s["models"] = [m for m in s["models"] if m["identifier"] != a[1]]
    s["unloads"] = s.get("unloads", 0) + 1
tmp = f"{path}.{os.getpid()}"
json.dump(s, open(tmp, "w"))
os.replace(tmp, path)
'''
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
        self.lms = self.tmp / "fake_lms.py"
        self.lms.write_text(FAKE_LMS)
        self.lms.chmod(0o755)

    def write(self, s):
        self.state.write_text(json.dumps(s))

    def read(self):
        return json.loads(self.state.read_text())

    def worker(self, delay, hold):
        return subprocess.Popen([sys.executable, "-c", WORKER, str(self.lms), str(delay), str(hold)],
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
