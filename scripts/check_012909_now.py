#!/usr/bin/env python3
"""012909ジョブの現在状態: 質問メッセージ・進行フェーズ・再開ログ。"""
from __future__ import annotations

import glob
import json
import os
import time

job_dir = glob.glob("/app/data/transcriptions/job_20260805_012909*")[0]

for name in ("question_message.txt", "answer_light_log.txt"):
    path = os.path.join(job_dir, name)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as handle:
            print(f"=== {name} (mtime {time.ctime(os.path.getmtime(path))}) ===")
            print(handle.read()[:600])

with open(os.path.join(job_dir, "progress.json"), encoding="utf-8") as f:
    progress = json.load(f)
print("=== progress ===")
print("overall:", progress.get("overall_status"), "phase:", progress.get("phase"))
for ev in (progress.get("events") or [])[-6:]:
    print(json.dumps(ev, ensure_ascii=False)[:180])

log_path = "/tmp/resume_012909.log"
if os.path.isfile(log_path):
    with open(log_path, encoding="utf-8", errors="replace") as handle:
        tail = handle.read()[-1200:]
    print("=== resume_012909.log tail ===")
    print(tail)

print("=== locks ===")
for path in glob.glob("/app/data/locks/*"):
    print(os.path.basename(path), int(time.time() - os.path.getmtime(path)), "sec old")
