#!/usr/bin/env python3
"""Apply Phase 10 step-3 ③ answers (ask_without_candidate) via pinpoint."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from line_answer_reflect import after_qa_path, ensure_after_qa_initialized, load_after_qa_text, save_after_qa_text  # noqa: E402
from pinpoint_answer_apply import apply_answers  # noqa: E402
from step3_anomaly_repair import repair_step3_anomaly_anchor  # noqa: E402

REPORT_NAME = "phase10_step3_apply_report.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply ③ answers to after_qa")
    parser.add_argument("--job-dir", help="Job directory")
    parser.add_argument("--job-glob", default="job_20260330_164142*")
    parser.add_argument("--input-root", default="data/transcriptions")
    parser.add_argument(
        "--answers-json",
        default="",
        help="Default: job_dir/answers_step3.json",
    )
    parser.add_argument(
        "--base",
        choices=("after_qa", "mechanical"),
        default="after_qa",
        help="Base transcript (default: merged_transcript_after_qa.txt)",
    )
    args = parser.parse_args()

    if args.job_dir:
        job_dir = Path(args.job_dir)
    else:
        job_dir = next((ROOT / args.input_root).glob(args.job_glob))

    answers_path = Path(args.answers_json) if args.answers_json else job_dir / "answers_step3.json"
    if not answers_path.is_file():
        print(f"missing {answers_path}", file=sys.stderr)
        return 1

    doc = json.loads(answers_path.read_text(encoding="utf-8"))
    answers = doc if isinstance(doc, list) else doc.get("answers") or []
    answers = [repair_step3_anomaly_anchor(dict(a)) for a in answers]

    if args.base == "mechanical":
        base_path = job_dir / "merged_transcript_mechanical.txt"
    else:
        ensure_after_qa_initialized(str(job_dir))
        base_path = Path(after_qa_path(str(job_dir)))
        if not base_path.is_file():
            base_path = job_dir / "merged_transcript_after_qa.txt"
    if not base_path.is_file():
        print(f"missing base {base_path}", file=sys.stderr)
        return 1

    text = base_path.read_text(encoding="utf-8")
    out, applied = apply_answers(text, answers)
    ok = sum(1 for a in applied if not a.get("error") and not a.get("skipped"))
    err = sum(1 for a in applied if a.get("error"))

    if args.base == "after_qa":
        save_after_qa_text(str(job_dir), out)
        ai_path = job_dir / "merged_transcript_ai.txt"
        ai_path.write_text(out, encoding="utf-8")

    report = {
        "job_id": job_dir.name,
        "applied_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "answers_path": str(answers_path),
        "base_path": str(base_path),
        "applied_ok": ok,
        "applied_error": err,
        "applied": applied,
    }
    (job_dir / REPORT_NAME).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({k: report[k] for k in ("job_id", "applied_ok", "applied_error")}, ensure_ascii=False))
    return 0 if err == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
