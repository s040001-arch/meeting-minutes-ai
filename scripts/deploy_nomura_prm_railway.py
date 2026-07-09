#!/usr/bin/env python3
"""Nomura PRM ジョブ向け: 変更ファイルを Railway にアップロードし修正を実行する。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from upload_py_to_railway import upload  # noqa: E402

RAILWAY = "railway.cmd"
SERVICE = "meeting-minutes-ai"

DEPLOY_FILES = [
    "recognition_batch.py",
    "integrated_questions.py",
    "run_job_once.py",
    "run_answer_light.py",
    "run_question_cycle_once.py",
    "recorrect_from_line_answer.py",
    "coherence_review.py",
    "learned_corrections_store.py",
    "mechanical_correct_text.py",
    "line_send_question.py",
    "transcript_paths.py",
    "export_minutes_to_google_docs.py",
    "reprocess_job.py",
    "readable_transcript.py",
    "generate_minutes_transcript.py",
    "generate_minutes_other_sections.py",
    "run_docs_hub_e2e.py",
    "scripts/record_nomura_prm_learnings.py",
    "scripts/apply_nomura_prm_reentry_fixes.py",
    "data/knowledge/learned_corrections.json",
]


def _ssh(cmd: str) -> int:
    r = subprocess.run([RAILWAY, "ssh", "-s", SERVICE, cmd])
    return r.returncode


def main() -> int:
    for rel in DEPLOY_FILES:
        local = ROOT / rel
        if not local.is_file():
            print(f"missing {local}", flush=True)
            return 1
        remote = f"/app/{rel.replace(chr(92), '/')}"
        upload(remote, local, service=SERVICE)

    steps = [
        "mkdir -p /app/data/knowledge",
        "cd /app && PYTHONPATH=/app python3 scripts/record_nomura_prm_learnings.py",
        "cd /app && PYTHONPATH=/app python3 scripts/apply_nomura_prm_reentry_fixes.py",
    ]
    for cmd in steps:
        print(f"=== {cmd} ===", flush=True)
        rc = _ssh(cmd)
        if rc != 0:
            return rc
    print("deploy_nomura_prm_railway_done", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
