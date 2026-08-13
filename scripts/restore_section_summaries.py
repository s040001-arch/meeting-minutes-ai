#!/usr/bin/env python3
"""Restore per-section transcript summaries and optionally republish Docs."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from minutes_transcript_sections import (
    generate_sectioned_transcript,
    write_sectioned_transcript_artifacts,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-dir", required=True)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()

    job_dir = Path(args.job_dir)
    transcript_path = job_dir / "merged_transcript_after_qa.txt"
    transcript = transcript_path.read_text(encoding="utf-8")
    annotated = generate_sectioned_transcript(
        job_dir=job_dir,
        transcript_text=transcript,
    )
    result = write_sectioned_transcript_artifacts(
        job_dir=job_dir,
        annotated_transcript=annotated,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not args.publish:
        return 0

    first_heading = next(
        line.removeprefix("### ").strip()
        for line in annotated.splitlines()
        if line.startswith("### ▼")
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    completed = subprocess.run(
        [
            "python3",
            str(ROOT / "reprocess_job.py"),
            "--job-dir",
            str(job_dir),
            "--from-step",
            "6.3",
            "--verify-pattern",
            first_heading,
            "--reason",
            "restore_transcript_section_summaries",
        ],
        cwd=ROOT,
        env=env,
        check=False,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
