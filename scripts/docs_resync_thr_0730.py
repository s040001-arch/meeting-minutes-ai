#!/usr/bin/env python3
"""THR(0730)ジョブ: 統合仕上げで整文を再生成し、議事録とGoogle Docを更新する。"""
from __future__ import annotations

import glob
import os
import subprocess
import sys

sys.path.insert(0, "/app")
os.chdir("/app")

INPUT_ROOT = "data/transcriptions"
job_dir = glob.glob(f"/app/{INPUT_ROOT}/job_20260730_025256*")[0]
job_id = os.path.basename(job_dir)
print("job:", job_id[:50], flush=True)

steps = [
    [
        sys.executable,
        "generate_minutes_transcript.py",
        "--job-id",
        job_id,
        "--input-root",
        INPUT_ROOT,
    ],
    [
        sys.executable,
        "generate_minutes_other_sections.py",
        "--job-id",
        job_id,
        "--input-root",
        INPUT_ROOT,
    ],
    [
        sys.executable,
        "run_docs_hub_e2e.py",
        "--job-id",
        job_id,
        "--input-root",
        INPUT_ROOT,
        "--push",
    ],
]
for cmd in steps:
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    print(f"=== {cmd[1]} exit={result.returncode} ===", flush=True)
    output = (result.stdout or "") + (result.stderr or "")
    for line in output.splitlines():
        if any(
            key in line
            for key in (
                "unified_finishing_done",
                "minutes_quality_gate",
                "minutes_source",
                "full_write_verified",
                "doc_url",
                "status=",
                "Error",
                "error",
            )
        ):
            print(line, flush=True)
    if result.returncode != 0:
        print(output[-2500:], flush=True)
        print("RESYNC_FAILED", flush=True)
        sys.exit(result.returncode)
print("RESYNC_DONE", flush=True)
