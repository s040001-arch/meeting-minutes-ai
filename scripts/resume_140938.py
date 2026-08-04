#!/usr/bin/env python3
"""楽天ジョブ(140938)の中断した回答反映再開を手動で再起動する。"""
from __future__ import annotations

import glob
import os
import subprocess

job_dirs = glob.glob("/app/data/transcriptions/job_20260804_140938*")
assert job_dirs, "job not found"
jid = os.path.basename(job_dirs[0])
log = open("/tmp/resume_manual.log", "w")
proc = subprocess.Popen(
    ["python3", "run_answer_light.py", "--job-id", jid, "--send-line"],
    cwd="/app",
    stdout=log,
    stderr=subprocess.STDOUT,
    start_new_session=True,
)
print("launched", proc.pid, jid[:40])
