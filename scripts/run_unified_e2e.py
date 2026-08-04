#!/usr/bin/env python3
"""統合仕上げパスのオフラインE2E検証（本番ジョブのファイルは変更しない）。

過去ジョブの after_qa 本文を入力に、UNIFIED_FINISHING_ENABLED=1 で
run_unified_finishing をスクラッチディレクトリ上で実行し、
品質ゲート評価（純関数）まで通して結果とかかった時間を報告する。
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time

sys.path.insert(0, os.getcwd())

JOB_PREFIXES = [
    "job_20260730_025256",  # THR 生成AI 土井様
    "job_20260709_025405",  # NREPT 國井様
]
INPUT_ROOT = "/app/data/transcriptions"
SCRATCH_ROOT = "/tmp/unified_e2e"


def find_job_dir(prefix: str) -> str:
    matches = glob.glob(os.path.join(INPUT_ROOT, prefix + "*"))
    if not matches:
        raise SystemExit(f"job not found: {prefix}")
    return matches[0]


def load_source_text(job_dir: str) -> str:
    for name in ("merged_transcript_after_qa.txt", "merged_transcript_ai.txt"):
        path = os.path.join(job_dir, name)
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                return handle.read()
    raise SystemExit(f"no transcript in {job_dir}")


def main() -> int:
    os.environ["UNIFIED_FINISHING_ENABLED"] = "1"
    from meeting_profile import load_meeting_profile
    from minutes_quality_gate import evaluate_minutes_quality
    from unified_finishing_pass import run_unified_finishing

    results = []
    for prefix in JOB_PREFIXES:
        job_dir = find_job_dir(prefix)
        text = load_source_text(job_dir)
        profile = load_meeting_profile(job_dir)
        scratch = os.path.join(SCRATCH_ROOT, prefix)
        os.makedirs(scratch, exist_ok=True)

        started = time.monotonic()
        out_text, stats, report = run_unified_finishing(
            job_dir=scratch,
            text=text,
            meeting_profile=profile,
        )
        elapsed = round(time.monotonic() - started, 1)

        readable_stats = {
            "total_chunks": 0,
            "failed_chunk_idx": [],
            "final_review": report,
            "unified_finishing": stats,
        }
        gate = evaluate_minutes_quality(
            text=out_text,
            readable_stats=readable_stats,
            correction_audit_rows=[],
            unknown_points=[],
        )
        with open(
            os.path.join(scratch, "output_text.txt"), "w", encoding="utf-8"
        ) as handle:
            handle.write(out_text)
        summary = {
            "job": os.path.basename(job_dir)[:40],
            "elapsed_sec": elapsed,
            "input_chars": stats["input_chars"],
            "output_chars": stats["output_chars"],
            "audit_windows": stats["audit_windows"],
            "audit_findings": stats["audit_findings"],
            "auto_applied": stats["auto_applied"],
            "remaining_findings": len(report.get("findings") or []),
            "failed": stats["failed"],
            "error": report.get("error"),
            "gate_status": gate["status"],
            "gate_blockers": [b["code"] for b in gate["blockers"]],
            "gate_warnings": [w["code"] for w in gate["warnings"]],
            "remaining_examples": [
                {
                    "quote": str(f.get("quote") or "")[:60],
                    "issue": str(f.get("issue") or "")[:60],
                    "confidence": f.get("confidence"),
                }
                for f in (report.get("findings") or [])[:10]
            ],
            "applied_examples": [
                {
                    "quote": str(f.get("quote") or "")[:50],
                    "fix": str(f.get("fix") or "")[:50],
                }
                for f in (report.get("applied") or [])[:10]
            ],
        }
        results.append(summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)

    with open(
        os.path.join(SCRATCH_ROOT, "e2e_results.json"), "w", encoding="utf-8"
    ) as handle:
        json.dump(results, handle, ensure_ascii=False, indent=2)
    print("E2E_DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
