#!/usr/bin/env python3
from __future__ import annotations

import io
import os
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from repo_env import load_dotenv_local  # noqa: E402
from run_answer_light import _mark_doc_completed, send_completion_line  # noqa: E402

JOB_ID = "job_20260709_025405_2026_0709_NREPT_國井様_村上様_山田様_相原"
INPUT_ROOT = "data/transcriptions"

load_dotenv_local()
job_dir = str(REPO / INPUT_ROOT / JOB_ID)
_mark_doc_completed(job_dir, JOB_ID, INPUT_ROOT, "")
send = bool(
    os.getenv("LINE_USER_ID", "").strip()
    and os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
)
print(send_completion_line(job_dir, job_id=JOB_ID, send_line=send))
