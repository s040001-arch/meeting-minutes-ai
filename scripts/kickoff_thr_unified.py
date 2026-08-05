#!/usr/bin/env python3
"""THRジョブを統合仕上げパスで本格処理し、最初の質問をLINEへ送る。

1. after_qa 本文へ統合仕上げ（監査→ピンポイント修正→検証）を実行し
   merged_transcript_readable.txt を更新する。
2. 品質ゲートを実行し、未解決問題を質問キューへ積む。
3. 質問サイクルを1回実行し、序盤の質問からLINEへ送信、pause状態にする。
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys

sys.path.insert(0, "/app")
os.chdir("/app")

INPUT_ROOT = "/app/data/transcriptions"
job_dir = glob.glob(os.path.join(INPUT_ROOT, "job_20260730_025256*"))[0]
job_id = os.path.basename(job_dir)
print("job:", job_id[:50])
print("UNIFIED_FINISHING_ENABLED =", os.environ.get("UNIFIED_FINISHING_ENABLED"))

from meeting_profile import load_meeting_profile  # noqa: E402
from readable_transcript import (  # noqa: E402
    generate_readable_transcript_with_stats,
)
from minutes_quality_gate import (  # noqa: E402
    MinutesQualityGateError,
    run_minutes_quality_gate,
)
from progress_tracker import update_job_progress  # noqa: E402
from question_mode import write_pause_marker  # noqa: E402

with open(
    os.path.join(job_dir, "merged_transcript_after_qa.txt"), encoding="utf-8"
) as handle:
    source_text = handle.read()
profile = load_meeting_profile(job_dir)

final_text, stats, out_path = generate_readable_transcript_with_stats(
    job_dir=job_dir,
    source_text=source_text,
    meeting_profile=profile,
)
unified = stats.get("unified_finishing") or {}
print(
    "unified stats:",
    json.dumps(
        {
            k: unified.get(k)
            for k in (
                "audit_windows",
                "audit_findings",
                "auto_applied",
                "queued_candidates",
                "verify_findings",
                "duration_sec",
                "failed",
                "answered_knowledge_items",
            )
        },
        ensure_ascii=False,
    ),
)

try:
    gate_report = run_minutes_quality_gate(
        job_dir=job_dir,
        text=final_text,
        readable_stats=stats,
    )
except MinutesQualityGateError:
    print("gate blocked (expected when questions remain)")
    with open(
        os.path.join(job_dir, "minutes_quality_gate.json"), encoding="utf-8"
    ) as handle:
        gate_report = json.load(handle)
status = (gate_report or {}).get("status")
blockers = [b.get("code") for b in (gate_report or {}).get("blockers", [])]
print("gate:", status, blockers)
queued = (gate_report or {}).get("metrics", {}).get(
    "final_review_questions_queued"
)
print("questions queued:", queued)

if status != "blocked":
    print("KICKOFF_DONE no_questions_needed")
    sys.exit(0)

qcycle = [
    sys.executable,
    "/app/run_question_cycle_once.py",
    "--job-id",
    job_id,
    "--input-root",
    INPUT_ROOT,
    "--unknowns",
    os.path.join(job_dir, "unknown_points.json"),
    "--text",
    out_path,
    "--send-line",
]
rc = subprocess.run(qcycle).returncode
print("question_cycle_exit:", rc)
if rc == 0:
    write_pause_marker(
        job_dir,
        mode=os.environ.get("QUESTION_MODE", "line"),
        question_artifacts=[
            "question_result.json",
            "unknown_points.json",
            "minutes_quality_gate.json",
        ],
        resume_hint="統合仕上げの質問回答後に軽量再開",
    )
    update_job_progress(
        input_root=INPUT_ROOT,
        job_id=job_id,
        phase="final_quality_question_pause",
        status="success",
        detail={"kickoff": "unified_thr"},
        overall_status="paused",
    )
print("KICKOFF_DONE")
