#!/usr/bin/env python3
"""Apply Phase 10 step-2 ② answers (ask_with_candidate) to mechanical transcript."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pinpoint_answer_apply import apply_answers  # noqa: E402

INPUT_MECHANICAL = "merged_transcript_mechanical.txt"
OUTPUT_AFTER_QA = "merged_transcript_after_qa.txt"
OUTPUT_AI = "merged_transcript_ai.txt"
REPORT_NAME = "phase10_step2_apply_report.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply Phase 10 step-2 ② answers")
    parser.add_argument("--job-dir", help="Job directory")
    parser.add_argument("--job-glob", default="job_20260330_164142*")
    parser.add_argument("--input-root", default="data/transcriptions")
    parser.add_argument(
        "--answers-json",
        default="",
        help="Override answers.json path (default: job_dir/answers.json)",
    )
    args = parser.parse_args()

    if args.job_dir:
        job_dir = Path(args.job_dir)
    else:
        job_dir = next((ROOT / args.input_root).glob(args.job_glob))

    mech_path = job_dir / INPUT_MECHANICAL
    if not mech_path.is_file():
        print(f"missing {mech_path}", file=sys.stderr)
        return 1

    answers_path = Path(args.answers_json) if args.answers_json else job_dir / "answers.json"
    answers = json.loads(answers_path.read_text(encoding="utf-8"))
    if isinstance(answers, dict):
        answers = answers.get("answers") or []

    text = mech_path.read_text(encoding="utf-8")
    out, applied = apply_answers(text, answers)
    ok = sum(1 for a in applied if not a.get("error"))
    err = sum(1 for a in applied if a.get("error"))

    (job_dir / OUTPUT_AFTER_QA).write_text(out, encoding="utf-8")
    (job_dir / OUTPUT_AI).write_text(out, encoding="utf-8")

    report = {
        "job_id": job_dir.name,
        "applied_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "input_path": str(mech_path),
        "input_chars": len(text),
        "output_chars": len(out),
        "delta_chars": len(out) - len(text),
        "answered_count": len([a for a in answers if str(a.get("answer_text") or "").strip()]),
        "applied_ok": ok,
        "applied_error": err,
        "applied": applied,
    }
    (job_dir / REPORT_NAME).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    fixture = ROOT / "scripts/fixtures/phase10_2_4_164142_answers_template.json"
    if fixture.is_file():
        doc = json.loads(fixture.read_text(encoding="utf-8"))
        by_qid = {a["question_id"]: a for a in doc.get("answers") or []}
        for rec in answers:
            qid = rec.get("question_id")
            if qid in by_qid:
                by_qid[qid]["answer_text"] = rec.get("answer_text", "")
        fixture.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({k: report[k] for k in ("job_id", "input_chars", "output_chars", "delta_chars", "applied_ok", "applied_error")}, ensure_ascii=False))
    for row in applied:
        if row.get("error"):
            print(f"ERROR review={row.get('review_index')} {row.get('anomaly_word')}: {row['error']}")
    return 0 if err == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
