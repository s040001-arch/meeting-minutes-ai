#!/usr/bin/env python3
"""Replay coherence routing BEFORE/AFTER Phase 3 on job anomalies or live re-detect."""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from coherence_review import (  # noqa: E402
    _coherence_to_unknown_points,
    run_coherence_review,
)
from recognition_batch import is_valid_coherence_question_word  # noqa: E402
from repo_env import load_dotenv_local  # noqa: E402


def _legacy_coherence_to_unknown_points(anomalies: list[dict]) -> list[dict]:
    """Pre-Phase-3 routing: medium + low + high(non-auto_fix)."""
    out: list[dict] = []
    for an in anomalies:
        conf = an.get("confidence")
        if conf == "high" and an.get("auto_fixable"):
            continue
        if conf not in {"high", "medium", "low"}:
            continue
        word = (an.get("anomaly_word") or "").strip()
        if not word or not is_valid_coherence_question_word(word):
            continue
        if len(word) > 30:
            continue
        if conf == "high" and not str(an.get("estimated_correction") or "").strip():
            continue
        out.append(an)
    return out


def _summarize(anomalies: list[dict], *, label: str) -> dict:
    legacy_q = _legacy_coherence_to_unknown_points(anomalies)
    new_q = _coherence_to_unknown_points(anomalies)
    tag_only = [
        a
        for a in anomalies
        if a.get("confidence") == "low"
        or (
            a.get("confidence") in {"medium", "low"}
            and (a.get("anomaly_word") or "") not in {q.get("anomaly_word") for q in new_q}
        )
    ]
    auto = [a for a in anomalies if a.get("auto_fixable")]
    return {
        "label": label,
        "total": len(anomalies),
        "question_count": len(new_q),
        "question_words": [
            {
                "word": q.get("anomaly_word"),
                "confidence": q.get("confidence"),
                "candidate": q.get("span_corrected") or q.get("estimated_correction") or "",
            }
            for q in new_q
        ],
        "legacy_question_count": len(legacy_q),
        "tag_only_count": len([a for a in anomalies if a.get("confidence") == "low"]),
        "tag_only_words": [a.get("anomaly_word") for a in anomalies if a.get("confidence") == "low"],
        "auto_fix_count": len(auto),
        "auto_fix_words": [a.get("anomaly_word") for a in auto],
    }


def _print_report(before: dict, after: dict) -> None:
    print("=== ROUTING REPLAY ===")
    print(f"BEFORE (legacy routing on same anomalies): questions={before['legacy_question_count']}")
    print(f"AFTER  (Phase 3 routing on same anomalies): questions={after['question_count']}")
    print(f"  tag-only (low): {after['tag_only_count']} -> {', '.join(after['tag_only_words'][:20])}")
    print(f"  auto_fix: {after['auto_fix_count']} -> {', '.join(after['auto_fix_words'])}")
    print("\n--- AFTER question queue ---")
    for i, q in enumerate(after["question_words"], 1):
        cand = q["candidate"]
        extra = f" -> {cand!r}" if cand else ""
        print(f"  {i:2d}. {q['word']} ({q['confidence']}){extra}")


def _filler_check(anomalies: list[dict], *, label: str) -> None:
    fillers = {"切れる", "かかってる", "こう力だからな", "できないかな", "任せていこう"}
    hits = [a.get("anomaly_word") for a in anomalies if a.get("anomaly_word") in fillers]
    print(f"\n=== FILLER CHECK ({label}) ===")
    print(f"  filler anomalies: {len(hits)} -> {hits if hits else '(none)'}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 3 coherence replay")
    parser.add_argument(
        "--anomalies-json",
        help="Path to transcript_anomalies.json (uses embedded anomalies list)",
    )
    parser.add_argument(
        "--job-id",
        help="Job id under data/transcriptions (for --redetect)",
    )
    parser.add_argument(
        "--input-root",
        default="data/transcriptions",
    )
    parser.add_argument(
        "--redetect",
        action="store_true",
        help="Re-run Opus coherence detection once (requires API key + job dir)",
    )
    args = parser.parse_args()
    load_dotenv_local()

    if args.redetect:
        if not args.job_id:
            print("--redetect requires --job-id", file=sys.stderr)
            return 1
        job_dir = os.path.join(args.input_root, args.job_id)
        result = run_coherence_review(job_dir)
        path = os.path.join(job_dir, "transcript_anomalies.json")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        new_anomalies = payload.get("anomalies") or []
        _filler_check(new_anomalies, label="new Opus detect")
        after = _summarize(new_anomalies, label="new detect + Phase3 routing")
        print(f"\nNew detect total={after['total']} questions={after['question_count']}")
        for i, q in enumerate(after["question_words"], 1):
            print(f"  {i:2d}. {q['word']} ({q['confidence']})")
        return 0

    if not args.anomalies_json:
        print("Provide --anomalies-json or --redetect --job-id", file=sys.stderr)
        return 1

    with open(args.anomalies_json, encoding="utf-8-sig") as f:
        payload = json.load(f)
    anomalies = payload.get("anomalies") if isinstance(payload, dict) else payload
    if not isinstance(anomalies, list):
        print("Invalid anomalies JSON", file=sys.stderr)
        return 1

    before = _summarize(anomalies, label="job snapshot BEFORE")
    after = _summarize(anomalies, label="Phase 3 routing AFTER")
    _print_report(before, after)
    _filler_check(anomalies, label="job snapshot anomalies")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
