#!/usr/bin/env python3
"""回答処理が生きているか確認する: プロセス・ロック・answers/unknownの状態。"""
from __future__ import annotations

import glob
import json
import os
import subprocess

print("=== processes ===")
try:
    out = subprocess.run(
        ["ps", "aux"], capture_output=True, text=True, timeout=10
    ).stdout
    for line in out.splitlines():
        if any(k in line for k in ("run_answer", "reprocess", "run_question", "recorrect", "generate_minutes")):
            print(line[:200])
except Exception as exc:  # noqa: BLE001
    print(f"ps_failed={exc!r}")

print("=== locks ===")
for path in glob.glob("/app/data/locks/*"):
    print(path, os.path.getmtime(path))

job_dir = glob.glob("/app/data/transcriptions/job_20260805_012909*")[0]

print("=== answers.json ===")
with open(os.path.join(job_dir, "answers.json"), encoding="utf-8") as f:
    answers = json.load(f)
print(json.dumps(answers, ensure_ascii=False)[:1500])

print("=== unknown statuses ===")
with open(os.path.join(job_dir, "unknown_points.json"), encoding="utf-8") as f:
    points = json.load(f)
from collections import Counter

print(Counter(str(p.get("status")) for p in points))

print("=== progress tail ===")
with open(os.path.join(job_dir, "progress.json"), encoding="utf-8") as f:
    progress = json.load(f)
print("overall:", progress.get("overall_status"), "phase:", progress.get("phase"))
for ev in (progress.get("events") or [])[-5:]:
    print(json.dumps(ev, ensure_ascii=False)[:200])
