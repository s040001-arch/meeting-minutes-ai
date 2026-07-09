#!/usr/bin/env python3
"""NREPT after_qa の残り相槌2件をパッチして再出力・完了通知。"""
from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from repo_env import load_dotenv_local  # noqa: E402
from run_answer_light import _mark_doc_completed, send_completion_line  # noqa: E402

JOB_ID = "job_20260709_025405_2026_0709_NREPT_國井様_村上様_山田様_相原"
INPUT_ROOT = "data/transcriptions"
EXTRA = [
    ("はいで、対象がL1の昇格", "対象がL1の昇格"),
    ("はいで、それで相原さんに", "それで相原さんに"),
]


def main() -> int:
    load_dotenv_local()
    job_dir = REPO / INPUT_ROOT / JOB_ID
    path = job_dir / "merged_transcript_after_qa.txt"
    text = path.read_text(encoding="utf-8")
    for old, new in EXTRA:
        if old in text:
            text = text.replace(old, new)
            print(f"patched: {old!r}")
    path.write_text(text, encoding="utf-8")

    env = os.environ.copy()
    env["FINAL_REVIEW_MODE"] = "apply"
    env["READABLE_TRANSCRIPT_ENABLED"] = "1"
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
            "nrept_extra_backchannel",
        ],
        cwd=str(REPO),
        env=env,
    ).returncode
    if rc != 0:
        return rc

    _mark_doc_completed(str(job_dir), JOB_ID, INPUT_ROOT, "")
    send = bool(
        os.getenv("LINE_USER_ID", "").strip()
        and os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    )
    print(send_completion_line(str(job_dir), job_id=JOB_ID, send_line=send))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
