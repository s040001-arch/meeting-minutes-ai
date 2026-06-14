#!/usr/bin/env python3
"""Phase 2 replay: materiality routing and auto-delete candidate listing."""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from correction_audit import (  # noqa: E402
    build_auto_delete_audit_entry,
    classify_anomaly_routing,
    is_auto_delete_candidate,
    normalize_content_nature,
    normalize_materiality,
    resolve_coherence_auto_delete_mode,
)
from coherence_review import _coherence_to_unknown_points  # noqa: E402

FILLER_WORDS = frozenset(
    {
        "切れる",
        "かかってる",
        "こう力だからな",
        "万歳",
    }
)

NATURE_BY_WORD = {
    "切れる": "metaphor",
    "かかってる": "backchannel",
    "こう力だからな": "colloquial",
    "万歳": "interjection",
}


def enrich_legacy_anomaly(an: dict) -> dict:
    """Conservative replay enrichment when Phase 2 fields are absent."""
    out = dict(an)
    word = str(out.get("anomaly_word") or "").strip()
    conf = str(out.get("confidence") or "low").lower()
    if not out.get("materiality"):
        if conf == "low" and word in FILLER_WORDS:
            out["materiality"] = "low"
            out["content_nature"] = NATURE_BY_WORD.get(word, "colloquial")
        else:
            out["materiality"] = "high"
            out["content_nature"] = "substantive" if conf in {"medium", "high"} else "unknown"
    out["materiality"] = normalize_materiality(out.get("materiality"))
    out["content_nature"] = normalize_content_nature(out.get("content_nature"))
    return out


def analyze(anomalies: list[dict], *, sample_text: str = "") -> dict:
    enriched = [enrich_legacy_anomaly(a) for a in anomalies]
    buckets: dict[str, list[dict]] = {
        "auto_delete_candidate": [],
        "question": [],
        "tag_only": [],
        "auto_fix": [],
    }
    for an in enriched:
        route = classify_anomaly_routing(an)
        entry = {
            "word": an.get("anomaly_word"),
            "confidence": an.get("confidence"),
            "materiality": an.get("materiality"),
            "content_nature": an.get("content_nature"),
            "reason": an.get("reason"),
            "span_text": an.get("span_text") or an.get("context"),
            "estimated_correction": an.get("estimated_correction") or an.get("span_corrected"),
        }
        if route == "auto_delete_candidate" and sample_text:
            audit = build_auto_delete_audit_entry(
                sample_text,
                an,
                mode=resolve_coherence_auto_delete_mode(),
                applied=False,
            )
            if audit:
                entry["restore_text"] = audit.get("restore_text") or audit.get("before")
                entry["delete_span"] = audit.get("before")
        buckets[route].append(entry)

    question_queue = _coherence_to_unknown_points(enriched)
    return {
        "counts": {k: len(v) for k, v in buckets.items()},
        "question_queue_count": len(question_queue),
        "buckets": buckets,
        "auto_delete_mode": resolve_coherence_auto_delete_mode(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 2 coherence replay")
    parser.add_argument(
        "--anomalies-json",
        default=os.path.join(
            ROOT,
            "scripts",
            "fixtures",
            "job_20260612_223435",
            "transcript_anomalies.before.json",
        ),
    )
    parser.add_argument(
        "--transcript",
        default=os.path.join(
            ROOT,
            "scripts",
            "fixtures",
            "job_20260612_223435",
            "merged_transcript_ai.txt",
        ),
    )
    parser.add_argument("--output", default=None, help="Write JSON report to file")
    args = parser.parse_args()

    with open(args.anomalies_json, encoding="utf-8") as f:
        payload = json.load(f)
    anomalies = payload.get("anomalies") if isinstance(payload, dict) else payload
    sample_text = ""
    if os.path.isfile(args.transcript):
        with open(args.transcript, encoding="utf-8") as f:
            sample_text = f.read()

    report = analyze(anomalies, sample_text=sample_text)
    out_json = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(out_json)
        print(f"wrote {args.output}")
    else:
        sys.stdout.buffer.write(out_json.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
