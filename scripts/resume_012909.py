#!/usr/bin/env python3
"""楽天ジョブ(012909)のデプロイで中断した回答反映再開を手動で再起動する。

前提確認もここで行う: 再開プロセスが本当に死んでいる場合のみ再起動する
（processing_visible_log / progress の更新が5分以上止まっていること）。
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import time

job_dirs = glob.glob("/app/data/transcriptions/job_20260805_012909*")
assert job_dirs, "job not found"
job_dir = job_dirs[0]
jid = os.path.basename(job_dir)

progress_path = os.path.join(job_dir, "progress.json")
age_sec = time.time() - os.path.getmtime(progress_path)
with open(progress_path, encoding="utf-8") as handle:
    progress = json.load(handle)
phase = str(progress.get("phase") or "")
print(f"phase={phase} progress_age_sec={int(age_sec)}")

if phase != "resume_after_question_pause" or age_sec < 240:
    print("SKIP: process appears alive or already past resume phase")
    raise SystemExit(0)

# デプロイ再起動で残った auto_after_answer ロックを掃除してから再起動
for lock in glob.glob("/app/data/locks/auto_after_answer_*.lock"):
    lock_age = time.time() - os.path.getmtime(lock)
    if lock_age > 240:
        os.unlink(lock)
        print(f"removed_stale_lock={os.path.basename(lock)} age={int(lock_age)}")

log = open("/tmp/resume_012909.log", "w")
proc = subprocess.Popen(
    ["python3", "run_answer_light.py", "--job-id", jid, "--send-line"],
    cwd="/app",
    stdout=log,
    stderr=subprocess.STDOUT,
    start_new_session=True,
)
print("launched", proc.pid, jid[:40])
