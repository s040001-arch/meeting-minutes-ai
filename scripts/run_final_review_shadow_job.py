#!/usr/bin/env python3
"""指定ジョブの整文に対して最終批評パスを shadow 実行しレポートを出力する。"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from final_review_pass import run_final_review  # noqa: E402
from readable_transcript import readable_transcript_path  # noqa: E402
from repo_env import load_dotenv_local  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run final review in shadow mode on a job")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--input-root", default="data/transcriptions")
    args = parser.parse_args()

    load_dotenv_local()
    os.environ["FINAL_REVIEW_MODE"] = "shadow"

    job_dir = REPO / args.input_root / args.job_id
    if not job_dir.is_dir():
        print(f"job_dir_missing={job_dir}")
        return 1

    readable = readable_transcript_path(str(job_dir))
    if not os.path.isfile(readable):
        after_qa = job_dir / "merged_transcript_after_qa.txt"
        if not after_qa.is_file():
            print("no readable or after_qa transcript")
            return 1
        readable = str(after_qa)
        print(f"fallback_input={readable}")

    with open(readable, encoding="utf-8") as f:
        text = f.read()

    _, report = run_final_review(job_dir=str(job_dir), text=text)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    findings = report.get("findings") or []
    print(f"shadow_summary findings={len(findings)} applied=0")
    for f in findings:
        print(
            f"  [{f.get('type')}/{f.get('confidence')}] "
            f"{str(f.get('quote', ''))[:55]!r}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
