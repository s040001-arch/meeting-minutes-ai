#!/usr/bin/env python3
"""Export Phase 10 step-3 ③ answers template (ask_without_candidate)."""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from edit_proposal_schema import (  # noqa: E402
    VERDICT_ASK_WITHOUT_CANDIDATE,
    align_proposal_spans_in_text,
    normalize_verdict,
    to_unknown_point,
)
from phase10_answer_template import build_ask_question_text, context_for_proposal  # noqa: E402
from question_bundle_step3 import (  # noqa: E402
    TIER_FACT_ERROR,
    TIER_LABELS,
    TIER_LOW,
    TIER_MATERIAL,
    bundle_safe_step3_items,
    prioritize_step3_items,
)
from step3_anomaly_repair import (
    audit_anomaly_span_alignment,
    detect_shiai_company_garble,
    expand_shiai_step3_items,
    repair_step3_anomaly_anchor,
)

JOB_GLOB_DEFAULT = "job_20260330_164142*"
INPUT_ROOT_DEFAULT = "data/transcriptions"


def export_step3_template(job_dir: Path) -> tuple[list[dict], dict]:
    mech_path = job_dir / "merged_transcript_mechanical.txt"
    props_path = job_dir / "edit_proposals.json"
    if not mech_path.is_file():
        raise FileNotFoundError(f"missing {mech_path}")
    if not props_path.is_file():
        raise FileNotFoundError(f"missing {props_path}")

    text = mech_path.read_text(encoding="utf-8")
    doc = json.loads(props_path.read_text(encoding="utf-8"))
    proposals = list(doc.get("proposals") or [])

    selected: list[dict] = []
    for raw in proposals:
        if normalize_verdict(raw.get("verdict")) != VERDICT_ASK_WITHOUT_CANDIDATE:
            continue
        p = dict(raw)
        align_proposal_spans_in_text(text, p)
        selected.append(p)

    answers: list[dict] = []
    for p in selected:
        qid = str(uuid.uuid4())
        span_before = str(p.get("span_before") or "").strip()
        context = context_for_proposal(p, text, n_before=2, n_after=2)
        try:
            unknown_point = to_unknown_point(p)
        except ValueError:
            unknown_point = {}

        entry = {
            "question_id": qid,
            "question_text": "",
            "answer_text": "",
            "answered_at": None,
            "verdict": VERDICT_ASK_WITHOUT_CANDIDATE,
            "proposal_id": p.get("proposal_id"),
            "anomaly_word": p.get("anomaly_word"),
            "span_before": span_before,
            "hypothesis": "",
            "context": context,
            "fact_class": p.get("fact_class"),
            "reason": p.get("reason"),
            "importance": p.get("importance"),
            "span_start": p.get("span_start"),
            "selected_unknown": unknown_point,
        }
        entry = repair_step3_anomaly_anchor(entry)
        if entry.get("anomaly_repair"):
            p_sync = {
                **p,
                "anomaly_word": entry.get("anomaly_word"),
                "fact_class": entry.get("fact_class"),
            }
            try:
                entry["selected_unknown"] = to_unknown_point(p_sync)
            except ValueError:
                pass
        entry["question_text"] = build_ask_question_text(entry)
        answers.append(entry)

    answers = expand_shiai_step3_items(answers)
    prioritized = prioritize_step3_items(answers)
    bundled = bundle_safe_step3_items(prioritized)

    tier_counts = Counter(int(x.get("priority_tier") or TIER_LOW) for x in prioritized)
    bundled_targets = sum(len(x.get("targets") or []) or 1 for x in bundled)

    tier_a = [a for a in bundled if int(a.get("priority_tier") or 99) == TIER_FACT_ERROR]
    alignment_audit: list[dict] = []
    for item in tier_a[:8]:
        issues = audit_anomaly_span_alignment(item)
        alignment_audit.append(
            {
                "review_index": item.get("review_index"),
                "anomaly_word": item.get("anomaly_word"),
                "span_before": item.get("span_before"),
                "issues": issues,
                "anomaly_repair": item.get("anomaly_repair"),
            }
        )

    meta = {
        "job_id": job_dir.name,
        "pipeline_editor_version": doc.get("pipeline_editor_version"),
        "verdict_filter": VERDICT_ASK_WITHOUT_CANDIDATE,
        "count_raw": len(prioritized),
        "count_after_bundle": len(bundled),
        "count_spans_after_bundle": bundled_targets,
        "bundle_safe_enabled": True,
        "priority_tiers": {
            TIER_LABELS[TIER_FACT_ERROR]: tier_counts.get(TIER_FACT_ERROR, 0),
            TIER_LABELS[TIER_MATERIAL]: tier_counts.get(TIER_MATERIAL, 0),
            TIER_LABELS[TIER_LOW]: tier_counts.get(TIER_LOW, 0),
        },
        "mechanical_chars": len(text),
        "mode": "③ ask_without_candidate — hypothesis 空。answer_text に正しい語を記入",
        "answer_examples": "正しい語 / 削除",
        "tier_a_alignment_audit": alignment_audit,
    }
    return bundled, meta


def write_markdown_review(path: Path, answers: list[dict], meta: dict) -> None:
    lines = [
        f"# Phase 10 Step 3 — ③候補なし確認 ({meta.get('job_id')})",
        "",
        f"- 生件数: **{meta.get('count_raw')}** → バンドル後 **{meta.get('count_after_bundle')}** 問（span **{meta.get('count_spans_after_bundle')}**）",
        f"- 優先度内訳: {meta.get('priority_tiers')}",
        f"- モード: **③** — `hypothesis` 空。`answer_text` に正しい語を記入（空欄=未回答）",
        f"- 回答例: `{meta.get('answer_examples')}`",
        "",
        "優先度 A（明らかな事実誤り）から順。各項目に **span_before** と **前後2文** を添付。",
        "",
        "---",
        "",
    ]
    for item in answers:
        idx = item.get("review_index")
        tier = item.get("priority_label") or item.get("priority_tier")
        signals = item.get("priority_signals") or []
        targets = item.get("targets")
        lines.append(f"## {idx}. [{tier}] {item.get('anomaly_word') or '(no word)'}")
        lines.append("")
        if targets:
            lines.append(f"- **バンドル**: {len(targets)} 箇所（同一 anomaly_word）")
        lines.append(f"- **fact_class**: {item.get('fact_class')}")
        if signals:
            lines.append(f"- **優先理由**: {'; '.join(signals)}")
        lines.append(f"- **Opus reason**: {item.get('reason') or ''}")
        lines.append("")
        lines.append("### span_before")
        if targets:
            for i, t in enumerate(targets, 1):
                lines.append(f"**{i}.**")
                lines.append(f"```\n{t.get('span_before')}\n```")
                lines.append("")
        else:
            lines.append(f"```\n{item.get('span_before')}\n```")
            lines.append("")
        lines.append("### 周辺文脈（前後2文）")
        if targets:
            for i, t in enumerate(targets, 1):
                lines.append(f"**{i}.**")
                lines.append(f"```\n{t.get('context') or item.get('context')}\n```")
                lines.append("")
        else:
            lines.append(f"```\n{item.get('context')}\n```")
            lines.append("")
        lines.append("### question_text")
        lines.append(f"> {item.get('question_text')}")
        lines.append("")
        lines.append(f"- `question_id`: `{item.get('question_id')}`")
        lines.append("")
        lines.append("---")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Export ③ answers.json template")
    parser.add_argument("--job-dir", help="Job directory path")
    parser.add_argument("--job-glob", default=JOB_GLOB_DEFAULT)
    parser.add_argument("--input-root", default=INPUT_ROOT_DEFAULT)
    parser.add_argument(
        "--fixture-out",
        default="scripts/fixtures/phase10_step3_164142_answers_template.json",
    )
    parser.add_argument(
        "--review-md-out",
        default="scripts/fixtures/phase10_step3_164142_answers_review.md",
    )
    args = parser.parse_args()

    if args.job_dir:
        job_dir = Path(args.job_dir)
    else:
        job_dir = next((ROOT / args.input_root).glob(args.job_glob))

    answers, meta = export_step3_template(job_dir)

    out_job = job_dir / "answers_step3.json"
    out_job.write_text(json.dumps(answers, ensure_ascii=False, indent=2), encoding="utf-8")

    fixture_path = ROOT / args.fixture_out
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    fixture_path.write_text(
        json.dumps({"meta": meta, "answers": answers}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    review_path = ROOT / args.review_md_out
    write_markdown_review(review_path, answers, meta)

    print(json.dumps({k: meta[k] for k in ("count_raw", "count_after_bundle", "priority_tiers")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
