#!/usr/bin/env python3
"""Finish Rakuten from the saved Opus draft using GPT verification only."""
from __future__ import annotations

import glob
import json
import os
import subprocess
from pathlib import Path

from minutes_quality_gate import run_minutes_quality_gate
from minutes_transcript_sections import (
    generate_sectioned_transcript,
    write_sectioned_transcript_artifacts,
)
from single_pass_independent_verifier import (
    verify_and_repair_until_stable,
    write_verifier_report,
)


def main() -> int:
    matches = glob.glob("/app/data/transcriptions/job_20260805_072038*")
    if len(matches) != 1:
        raise RuntimeError(f"expected one Rakuten job, got {matches}")
    job_dir = Path(matches[0])
    raw = (job_dir / "merged_transcript.txt").read_text(encoding="utf-8")
    staged = (job_dir / "merged_transcript_after_qa.txt").read_text(
        encoding="utf-8"
    )
    verified, report, repairs = verify_and_repair_until_stable(
        raw_text=raw,
        edited_text=staged,
        job_dir=job_dir,
    )
    write_verifier_report(job_dir, report)
    print(f"verifier_status={report.get('status')}")
    print(f"verifier_repairs={len(repairs)}")
    if report.get("status") != "pass":
        print(json.dumps(report.get("findings") or [], ensure_ascii=False))
        return 2

    # Run the fail-closed gate before replacing publishable artifacts.
    stats = {
        "enabled": True,
        "single_pass_primary": True,
        "failed_chunk_idx": [],
        "total_chunks": 1,
        "final_review": {
            "mode": "apply",
            "model": report.get("model"),
            "findings": [],
            "applied": repairs,
            "error": report.get("error"),
        },
    }
    run_minutes_quality_gate(
        job_dir=str(job_dir),
        text=verified,
        readable_stats=stats,
    )

    (job_dir / "merged_transcript_after_qa.txt").write_text(
        verified.rstrip() + "\n",
        encoding="utf-8",
    )
    annotated = generate_sectioned_transcript(
        job_dir=job_dir,
        transcript_text=verified,
    )
    section_result = write_sectioned_transcript_artifacts(
        job_dir=job_dir,
        annotated_transcript=annotated,
    )
    print(f"section_headings={section_result.get('heading_count')}")

    env = dict(os.environ)
    env["PYTHONPATH"] = "/app"
    result = subprocess.run(
        [
            "python3",
            "/app/reprocess_job.py",
            "--job-dir",
            str(job_dir),
            "--from-step",
            "6.3",
            "--verify-pattern",
            "楽天インサイトは全員クロードコードとCodexも使える。",
            "--reason",
            "finish_from_staged_opus_gpt_verified_20260807",
        ],
        cwd="/app",
        env=env,
        check=False,
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
