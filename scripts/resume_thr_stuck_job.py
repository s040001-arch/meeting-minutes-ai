#!/usr/bin/env python3
"""Resume thr job stuck at 回答待ち after premature completion LINE."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from progress_tracker import update_job_progress
from question_mode import clear_pause_marker
from repo_env import load_dotenv_local

JOB_ID = "job_20260705_055804_2026_0624_thr社_運営改善_西脇様_竹中様_福田_相原"
INPUT_ROOT = "data/transcriptions"
REPO = Path(__file__).resolve().parents[1]


def main() -> int:
    load_dotenv_local()
    job_dir = REPO / INPUT_ROOT / JOB_ID
    if not job_dir.is_dir():
        print(f"missing {job_dir}")
        return 1

    rc = subprocess.run(
        [
            sys.executable,
            str(REPO / "reprocess_job.py"),
            "--job-dir",
            str(job_dir),
            "--input-root",
            INPUT_ROOT,
            "--from-step",
            "resume",
            "--reason",
            "thr_stuck_after_completion_line",
        ],
        cwd=str(REPO),
    ).returncode
    if rc != 0:
        return rc

    clear_pause_marker(str(job_dir))
    from run_answer_light import _mark_doc_completed, send_completion_line

    _mark_doc_completed(str(job_dir), JOB_ID, INPUT_ROOT, str(job_dir / "resume_fix.log"))
    send_line = bool(
        os.getenv("LINE_USER_ID", "").strip()
        and os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    )
    send_completion_line(str(job_dir), job_id=JOB_ID, send_line=False)

    update_job_progress(
        input_root=INPUT_ROOT,
        job_id=JOB_ID,
        phase="done",
        status="success",
        detail={"reason": "thr_stuck_recovery"},
        overall_status="success",
    )
    print("thr_job_recovered")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
