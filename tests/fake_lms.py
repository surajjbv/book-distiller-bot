#!/usr/bin/env python3
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
