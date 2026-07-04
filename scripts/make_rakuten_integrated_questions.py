#!/usr/bin/env python3
"""楽天インサイト社 fixture 向けラッパ（本体は integrated_questions.py）。

入力:
  scripts/fixtures/job_20260701_053826_edit_proposals.json
  scripts/fixtures/reader_pass_20260701_questions.md または reader_pass_result.json
  scripts/fixtures/job_20260701_053826_ai.txt

出力:
  scripts/fixtures/rakuten_integrated_questions.md
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from integrated_questions import (  # noqa: E402
    build_cascade_questions,
    build_integrated_md,
    load_ask_proposals,
    load_reader_pass_findings,
)

FIXTURES = REPO / "scripts" / "fixtures"
JOB_STAGING = FIXTURES / "_rakuten_integrated_staging"
OUTPUT_PATH = FIXTURES / "rakuten_integrated_questions.md"


def main() -> int:
    JOB_STAGING.mkdir(parents=True, exist_ok=True)
    # Stage job-shaped directory so the generic loader works.
    shutil.copy(
        FIXTURES / "job_20260701_053826_edit_proposals.json",
        JOB_STAGING / "edit_proposals.json",
    )
    rp_md = FIXTURES / "reader_pass_20260701_questions.md"
    if rp_md.is_file():
        shutil.copy(rp_md, JOB_STAGING / "reader_pass_questions.md")
    ai = FIXTURES / "job_20260701_053826_ai.txt"
    if ai.is_file():
        shutil.copy(ai, JOB_STAGING / "merged_transcript_ai.txt")

    proposals = load_ask_proposals(JOB_STAGING)
    findings = load_reader_pass_findings(JOB_STAGING)
    transcript = (JOB_STAGING / "merged_transcript_ai.txt").read_text(encoding="utf-8")
    cascade = build_cascade_questions(proposals)
    md, stats = build_integrated_md(
        job_id="job_20260701_053826_楽天インサイト社",
        reader_findings=findings,
        cascade_questions=cascade,
        transcript=transcript,
        resume_hint=(
            "python reprocess_job.py --job-dir <job> --from-step resume"
        ),
    )
    # Keep historical section titles for this fixture by light rewrite
    md = md.replace("# 統合質問シート", "# 楽天インサイト社 統合質問シート", 1)
    OUTPUT_PATH.write_text(md, encoding="utf-8")
    print(f"生成完了: {OUTPUT_PATH}")
    print(json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
