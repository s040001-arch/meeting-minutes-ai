#!/usr/bin/env python3
"""Print Phase 3 replay report as UTF-8 JSON."""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from repo_env import load_dotenv_local  # noqa: E402
from coherence_review import _coherence_to_unknown_points, run_coherence_review  # noqa: E402
from scripts.replay_coherence_phase3 import (  # noqa: E402
    _filler_check,
    _legacy_coherence_to_unknown_points,
    _summarize,
)

FIXTURE = os.path.join(ROOT, "scripts", "fixtures", "job_20260612_223435")
FILLERS = {"切れる", "かかってる", "こう力だからな", "できないかな", "任せていこう"}


def _filler_words(anomalies: list[dict]) -> list[str]:
    return [str(a.get("anomaly_word") or "") for a in anomalies if a.get("anomaly_word") in FILLERS]


def main() -> int:
    load_dotenv_local()
    with open(os.path.join(FIXTURE, "transcript_anomalies.before.json"), encoding="utf-8") as f:
        before_anomalies = json.load(f)["anomalies"]

    routing_before = _summarize(before_anomalies, label="legacy")
    routing_after_same = _summarize(before_anomalies, label="phase3_routing_only")

    run_coherence_review(FIXTURE)
    with open(os.path.join(FIXTURE, "transcript_anomalies.json"), encoding="utf-8") as f:
        new_anomalies = json.load(f)["anomalies"]
    detect_after = _summarize(new_anomalies, label="new_prompt_plus_routing")

    report = {
        "job": "job_20260612_223435",
        "routing_replay_on_original_30": {
            "legacy_questions": routing_before["legacy_question_count"],
            "phase3_routing_questions": routing_after_same["question_count"],
            "phase3_tag_only_low": routing_after_same["tag_only_count"],
            "question_words": routing_after_same["question_words"],
            "tag_only_words": routing_after_same["tag_only_words"],
            "auto_fix": routing_after_same["auto_fix_words"],
        },
        "filler_on_original_30": _filler_words(before_anomalies),
        "new_opus_detect": {
            "total_anomalies": detect_after["total"],
            "medium": sum(1 for a in new_anomalies if a.get("confidence") == "medium"),
            "low": sum(1 for a in new_anomalies if a.get("confidence") == "low"),
            "questions": detect_after["question_count"],
            "question_words": detect_after["question_words"],
            "tag_only_words": detect_after["tag_only_words"],
            "fillers_detected": _filler_words(new_anomalies),
        },
    }
    out_path = os.path.join(FIXTURE, "phase3_replay_report.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
