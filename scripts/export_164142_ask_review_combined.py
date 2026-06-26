#!/usr/bin/env python3
"""Generate a Cursor-ready review MD for all ②③ proposals from 164142.

Reads edit_proposals.json (85 items) + merged_transcript_mechanical.txt,
filters ask_with_candidate(②) and ask_without_candidate(③), scores by
materiality, bundles same-target items, and writes a single markdown with
empty answer fields for 相原 to fill in.

Source files are NOT modified.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from edit_proposal_schema import (  # noqa: E402
    VERDICT_ASK_WITH_CANDIDATE,
    VERDICT_ASK_WITHOUT_CANDIDATE,
    align_proposal_spans_in_text,
    normalize_verdict,
)
from phase10_answer_template import context_for_proposal, highlight_anomaly  # noqa: E402
from question_bundle import bundle_safe_answer_items  # noqa: E402
from question_bundle_step3 import (  # noqa: E402
    TIER_FACT_ERROR,
    TIER_LABELS,
    TIER_LOW,
    bundle_safe_step3_items,
    score_step3_materiality,
)

JOB_DIR = ROOT / "data/transcriptions/job_20260330_164142_2026_0325_プレセナ社_THR向け仕事力サーベイ_高田_秋本_季央_工藤"
OUT_PATH = ROOT / "docs/phase10_164142_ask_review_combined.md"


def _sort_key(item: dict) -> tuple:
    targets = item.get("targets") or [item]
    min_span = min(int(t.get("span_start") or -1) for t in targets)
    return (
        int(item.get("priority_tier") or TIER_LOW),
        int(item.get("priority_rank") or 50),
        min_span,
    )


def _verdict_label(item: dict) -> str:
    v = normalize_verdict(
        (item.get("selected_unknown") or {}).get("verdict") or item.get("verdict") or ""
    )
    if v == VERDICT_ASK_WITH_CANDIDATE:
        return "②"
    if v == VERDICT_ASK_WITHOUT_CANDIDATE:
        return "③"
    return v or "?"


def _apply_bundle_priority(bundled: list[dict], originals: list[dict]) -> None:
    """Back-fill best priority_tier/rank onto ② bundles (bundle_safe_answer_items
    takes first-item's score; recalculate from all grouped originals)."""
    prop_ids: dict[str, dict] = {str(p.get("proposal_id")): p for p in originals}
    for item in bundled:
        targets = item.get("targets") or []
        if not targets:
            continue
        group_tiers = []
        group_ranks = []
        for t in targets:
            pid = str(t.get("proposal_id") or "")
            src = prop_ids.get(pid)
            if src:
                group_tiers.append(int(src.get("priority_tier") or TIER_LOW))
                group_ranks.append(int(src.get("priority_rank") or 50))
        if group_tiers:
            item["priority_tier"] = min(group_tiers)
            item["priority_rank"] = min(group_ranks)
            item["priority_label"] = TIER_LABELS.get(item["priority_tier"], str(item["priority_tier"]))


def _write_markdown(path: Path, items: list[dict], meta: dict) -> None:
    job_id = meta["job_id"]
    n2 = meta["n_ask2_raw"]
    n3 = meta["n_ask3_raw"]
    n_bundled = len(items)
    lines = [
        f"# 164142 ②③ 回答待ち一覧（相原レビュー用）",
        "",
        f"- ジョブ: `{job_id}`",
        f"- 対象: ② {n2}件 + ③ {n3}件 = {n2+n3}件 → バンドル後 **{n_bundled}問**",
        "- 優先度: **A** = 明らかな事実誤り / **B** = 本文影響あり / **C** = 低材料性",
        "- `### 回答` 欄に正しい語 / 削除 / スキップ を記入して保存（transcript・proposals は変更しない）",
        "",
        "---",
        "",
    ]

    for item in items:
        idx = item.get("review_index", "?")
        tier_label = item.get("priority_label") or TIER_LABELS.get(
            int(item.get("priority_tier") or TIER_LOW), "?"
        )
        aw = str(item.get("anomaly_word") or "(no word)").strip()
        vlabel = _verdict_label(item)
        targets = item.get("targets") or []
        n_targets = len(targets)
        count_str = f" × {n_targets}件" if n_targets > 1 else ""

        lines.append(f"## {idx}. [{tier_label}] {aw} {vlabel}{count_str}")
        lines.append("")
        lines.append(f"- **verdict**: {vlabel} `{normalize_verdict(item.get('verdict') or '')}`")
        lines.append(f"- **fact_class**: `{item.get('fact_class') or ''}`")

        hyp = str(item.get("hypothesis") or "").strip()
        if vlabel == "②" and hyp:
            lines.append(f"- **hypothesis**: `{hyp}`")
        else:
            lines.append("- **hypothesis**: —")

        signals = item.get("priority_signals") or []
        if signals:
            lines.append(f"- **優先理由**: {'; '.join(signals)}")

        reason = str(item.get("reason") or "").strip()
        if reason:
            lines.append(f"- **Opus reason**: {reason}")
        lines.append("")

        if targets:
            lines.append(f"### span_before（{n_targets}箇所）")
            lines.append("")
            for i, t in enumerate(targets, 1):
                sb = str(t.get("span_before") or "").strip()
                highlighted = highlight_anomaly(sb, aw)
                lines.append(f"**{i}.**")
                lines.append(f"```")
                lines.append(highlighted)
                lines.append("```")
                lines.append("")
            lines.append(f"### 周辺文脈（前後2文）")
            lines.append("")
            for i, t in enumerate(targets, 1):
                ctx = str(t.get("context") or item.get("context") or "").strip()
                lines.append(f"**{i}.**")
                lines.append("```")
                lines.append(ctx)
                lines.append("```")
                lines.append("")
        else:
            sb = str(item.get("span_before") or "").strip()
            highlighted = highlight_anomaly(sb, aw)
            lines.append("### span_before")
            lines.append("")
            lines.append("```")
            lines.append(highlighted)
            lines.append("```")
            lines.append("")
            ctx = str(item.get("context") or "").strip()
            lines.append("### 周辺文脈（前後2文）")
            lines.append("")
            lines.append("```")
            lines.append(ctx)
            lines.append("```")
            lines.append("")

        lines.append("### 回答")
        lines.append("")
        if vlabel == "②" and hyp:
            lines.append(f"> ②候補: 「{hyp}」→ 「正しい」か別の語か削除を記入")
            lines.append(">")
        lines.append("> *(正しい語 / 削除 / スキップ)*")
        lines.append("")
        lines.append("---")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    props_path = JOB_DIR / "edit_proposals.json"
    mech_path = JOB_DIR / "merged_transcript_mechanical.txt"

    if not props_path.is_file():
        raise SystemExit(f"proposals not found: {props_path}")
    if not mech_path.is_file():
        raise SystemExit(f"transcript not found: {mech_path}")

    doc = json.loads(props_path.read_text(encoding="utf-8"))
    proposals = list(doc.get("proposals") or [])
    text = mech_path.read_text(encoding="utf-8")

    # Filter ②③, align spans, extract context, score materiality
    ask2_raw: list[dict] = []
    ask3_raw: list[dict] = []
    for raw in proposals:
        v = normalize_verdict(raw.get("verdict") or "")
        if v not in (VERDICT_ASK_WITH_CANDIDATE, VERDICT_ASK_WITHOUT_CANDIDATE):
            continue
        p = dict(raw)
        align_proposal_spans_in_text(text, p)
        p["context"] = context_for_proposal(p, text, n_before=2, n_after=2)
        p.update(score_step3_materiality(p))
        if v == VERDICT_ASK_WITH_CANDIDATE:
            ask2_raw.append(p)
        else:
            ask3_raw.append(p)

    print(f"filtered: ② {len(ask2_raw)}, ③ {len(ask3_raw)}", flush=True)

    # Bundle independently
    bundled2 = bundle_safe_answer_items(ask2_raw)
    _apply_bundle_priority(bundled2, ask2_raw)

    bundled3 = bundle_safe_step3_items(ask3_raw)

    # Attach context to each target in bundles (bundle functions use proposal_to_target
    # which copies span_before/span_start but not context)
    ctx_by_pid: dict[str, str] = {
        str(p.get("proposal_id")): str(p.get("context") or "") for p in ask2_raw + ask3_raw
    }
    for item in bundled2 + bundled3:
        for t in item.get("targets") or []:
            pid = str(t.get("proposal_id") or "")
            if pid and not t.get("context"):
                t["context"] = ctx_by_pid.get(pid, "")
        if not item.get("context"):
            pid = str(item.get("proposal_id") or "")
            item["context"] = ctx_by_pid.get(pid, "")

    # Merge and re-sort by (tier, rank, min_span_start); re-index
    all_items = sorted(bundled2 + bundled3, key=_sort_key)
    for i, it in enumerate(all_items, 1):
        it["review_index"] = i

    n_bundled = len(all_items)
    print(f"bundled: ② {len(bundled2)}, ③ {len(bundled3)} → total {n_bundled}", flush=True)

    # Spot-check tier A count
    tier_a = sum(1 for it in all_items if int(it.get("priority_tier") or TIER_LOW) == TIER_FACT_ERROR)
    print(f"tier A (明らかな事実誤り): {tier_a}", flush=True)

    meta = {
        "job_id": JOB_DIR.name,
        "n_ask2_raw": len(ask2_raw),
        "n_ask3_raw": len(ask3_raw),
    }
    _write_markdown(OUT_PATH, all_items, meta)
    print(f"wrote: {OUT_PATH}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
