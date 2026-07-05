#!/usr/bin/env python3
"""thr最終ポリッシュ追補: 取り残した2箇所を修正して resume・完了処理。"""
from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

JOB_ID = "job_20260705_055804_2026_0624_thr社_運営改善_西脇様_竹中様_福田_相原"
INPUT_ROOT = "data/transcriptions"

FIXES = [
    ("私たちの魔法石だったとして、あの研修屋をなんだ。脱却したいみたいな",
     "私たちの方針としても、あの研修屋をなんとか脱却したいみたいな"),
    ("はいぜ、ちょっと前向きに検討、あのありがとうございます",
     "はい、ぜひちょっと前向きに検討、あのありがとうございます"),
]


def main() -> int:
    job_dir = REPO / INPUT_ROOT / JOB_ID
    path = job_dir / "merged_transcript_after_qa.txt"
    text = path.read_text(encoding="utf-8")
    n = 0
    for old, new in FIXES:
        c = text.count(old)
        if c == 0:
            print(f"NOT_FOUND: {old[:40]!r}")
            continue
        text = text.replace(old, new)
        n += c
        print(f"fix x{c}: {old[:30]!r}")
    if n == 0:
        print("nothing applied")
        return 1
    path.write_text(text, encoding="utf-8")
    print(f"after_qa_saved fixes={n}")

    from repo_env import load_dotenv_local

    load_dotenv_local()
    env = os.environ.copy()
    env["QUESTION_MODE"] = env.get("QUESTION_MODE") or "line"
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
            "thr_polish_addendum",
        ],
        cwd=str(REPO),
        env=env,
    ).returncode
    print(f"resume_exit={rc}")
    if rc != 0:
        return rc

    from progress_tracker import update_job_progress

    update_job_progress(
        input_root=INPUT_ROOT,
        job_id=JOB_ID,
        phase="done",
        status="success",
        detail={"reason": "thr_polish_addendum", "applied": n},
        overall_status="success",
    )
    print("thr_polish_addendum_done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
