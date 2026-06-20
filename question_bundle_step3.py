"""Priority + safe bundling for Phase 10 step-3 ③ (ask_without_candidate)."""
from __future__ import annotations

import re
import uuid
from typing import Any

from edit_proposal_schema import VERDICT_ASK_WITHOUT_CANDIDATE, normalize_verdict
from question_bundle import (
    BUNDLE_KIND_REPLACE,
    build_bundled_replace_question_text,
    build_single_replace_question_text,
    effective_landing,
    normalize_hypothesis,
    proposal_to_target,
)
from step3_anomaly_repair import GARBLE_PATTERN_SHIAI_SHA, SHIAI_GARBLE_TOKEN

TIER_FACT_ERROR = 1
TIER_MATERIAL = 2
TIER_LOW = 3

TIER_LABELS = {
    TIER_FACT_ERROR: "A_明らかな事実誤り",
    TIER_MATERIAL: "B_本文影響あり",
    TIER_LOW: "C_低材料性",
}


def normalize_anomaly_word(word: str) -> str:
    return re.sub(r"\s+", "", str(word or "").strip())


def score_step3_materiality(item: dict[str, Any]) -> dict[str, Any]:
    """Context-based priority for ③ (not a fixed vocabulary list)."""
    fc = str(item.get("fact_class") or "").strip()
    aw = str(item.get("anomaly_word") or "").strip()
    sb = str(item.get("span_before") or "").strip()
    ctx = str(item.get("context") or "").strip()
    reason = str(item.get("reason") or "").strip()

    tier = TIER_LOW
    rank = 50
    signals: list[str] = []

    if re.search(r"\d+\s*数字", f"{aw} {sb}"):
        tier = TIER_FACT_ERROR
        rank = min(rank, 1)
        signals.append("数値+「数字」型（8数字型）")
    if re.search(r"\d+\s*車", sb) and re.search(r"社|子会社|\d+\s*社", ctx):
        tier = TIER_FACT_ERROR
        rank = min(rank, 3)
        signals.append("社数文脈での「N車」")
    if fc == "numeric":
        tier = min(tier, TIER_FACT_ERROR)
        rank = min(rank, 10)
        signals.append("fact_class=numeric")
    if fc == "datetime":
        tier = min(tier, TIER_MATERIAL)
        rank = min(rank, 25)
        signals.append("fact_class=datetime")
    if fc == "proper_noun":
        tier = min(tier, TIER_MATERIAL)
        rank = min(rank, 30)
        signals.append("fact_class=proper_noun")
        if re.search(r"[A-Za-z]{2,}", aw):
            tier = TIER_FACT_ERROR
            rank = min(rank, 12)
            signals.append("英字名の誤認識疑い")
    if fc == "uncertain":
        tier = min(tier, TIER_MATERIAL)
        rank = min(rank, 35)
        if re.search(r"数値|社数|人名|会社名|金額|万円", reason):
            tier = min(tier, TIER_FACT_ERROR)
            rank = min(rank, 15)
            signals.append("uncertainだが数値/固有名詞文脈")
    if re.search(r"社員数|サイン数|\d+\s*人", sb + reason):
        tier = min(tier, TIER_FACT_ERROR)
        rank = min(rank, 5)
        signals.append("人数・社員数の属性語崩れ")
    if fc == "filler_garble" and tier == TIER_LOW:
        rank = max(rank, 85)
        signals.append("filler_garble（口語崩れ優先度低）")

    if not signals:
        signals.append("一般の文脈不整合")

    return {
        "priority_tier": tier,
        "priority_rank": rank,
        "priority_label": TIER_LABELS[tier],
        "priority_signals": signals,
    }


def _can_safe_merge_step3_group(items: list[dict[str, Any]]) -> bool:
    """Bundle ③ only when the same garbled token repeats with same fact_class."""
    if len(items) < 2:
        return False
    anomalies = [str(it.get("anomaly_word") or "").strip() for it in items]
    if len(set(anomalies)) != 1:
        return False
    fcs = {str(it.get("fact_class") or "").strip() for it in items}
    if len(fcs) != 1:
        return False
    answered = [str(it.get("answer_text") or "").strip() for it in items]
    if any(answered):
        landings = {effective_landing(a, "") for a in answered if a}
        if len(landings) != 1:
            return False
    return True


def build_bundled_shiai_question_text(bundle: dict[str, Any]) -> str:
    targets = list(bundle.get("targets") or [])
    lines: list[str] = []
    for i, t in enumerate(targets, 1):
        span_before = str(t.get("span_before") or "").strip()
        lines.append(f"{i}.「{span_before}」")
    joined = "\n".join(lines)
    return (
        "以下の「N 試合」はいずれも会社数の「N 社」の誤変換では?\n"
        f"{joined}\n"
        f"すべて「{SHIAI_GARBLE_TOKEN}」→同じ語（例: 社）ならその語を、"
        "箇所ごとに違うなら番号付きで返信、不要なら「削除」と返信してください。"
    )


def bundle_shiai_garble_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bundle 試合→社 garble items (② THR型: one answer, multiple spans)."""
    shiai = [it for it in items if it.get("garble_pattern") == GARBLE_PATTERN_SHIAI_SHA]
    rest = [it for it in items if it.get("garble_pattern") != GARBLE_PATTERN_SHIAI_SHA]
    if len(shiai) < 2:
        for it in shiai:
            it.pop("targets", None)
            if not str(it.get("question_text") or "").strip():
                it["question_text"] = build_single_replace_question_text(it)
        return rest + shiai

    answered = [str(it.get("answer_text") or "").strip() for it in shiai if str(it.get("answer_text") or "").strip()]
    if answered:
        landings = {effective_landing(a, "") for a in answered}
        if len(landings) != 1:
            for it in shiai:
                it.pop("targets", None)
                it["question_text"] = build_single_replace_question_text(it)
            return rest + shiai

    first = dict(shiai[0])
    targets = [proposal_to_target(g) for g in shiai]
    tiers = [int(g.get("priority_tier") or TIER_LOW) for g in shiai]
    ranks = [int(g.get("priority_rank") or 50) for g in shiai]
    first["question_id"] = str(first.get("question_id") or uuid.uuid4())
    first["bundle_kind"] = BUNDLE_KIND_REPLACE
    first["garble_pattern"] = GARBLE_PATTERN_SHIAI_SHA
    first["targets"] = targets
    first["anomaly_word"] = SHIAI_GARBLE_TOKEN
    first["priority_tier"] = min(tiers)
    first["priority_rank"] = min(ranks)
    first["priority_label"] = TIER_LABELS[first["priority_tier"]]
    first["question_text"] = build_bundled_shiai_question_text(first)
    return rest + [first]


def bundle_safe_step3_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group repeated ③ items (same anomaly_word); unmergeable stay single."""
    step3 = [
        it
        for it in items
        if normalize_verdict(
            (it.get("selected_unknown") or {}).get("verdict")
            or it.get("verdict")
            or VERDICT_ASK_WITHOUT_CANDIDATE
        )
        == VERDICT_ASK_WITHOUT_CANDIDATE
    ]
    by_anomaly: dict[str, list[dict[str, Any]]] = {}
    for it in step3:
        key = normalize_anomaly_word(str(it.get("anomaly_word") or ""))
        if not key:
            key = f"__id_{it.get('proposal_id')}"
        by_anomaly.setdefault(key, []).append(it)

    bundled: list[dict[str, Any]] = []
    for group in by_anomaly.values():
        if _can_safe_merge_step3_group(group):
            first = dict(group[0])
            targets = [proposal_to_target(g) for g in group]
            tiers = [int(g.get("priority_tier") or TIER_LOW) for g in group]
            ranks = [int(g.get("priority_rank") or 50) for g in group]
            bundle_tier = min(tiers) if tiers else TIER_LOW
            bundle_rank = min(ranks) if ranks else 50
            signals: list[str] = []
            for g in group:
                for s in g.get("priority_signals") or []:
                    if s not in signals:
                        signals.append(s)
            first["question_id"] = str(first.get("question_id") or uuid.uuid4())
            first["bundle_kind"] = BUNDLE_KIND_REPLACE
            first["targets"] = targets
            first["priority_tier"] = bundle_tier
            first["priority_rank"] = bundle_rank
            first["priority_label"] = TIER_LABELS[bundle_tier]
            first["priority_signals"] = signals
            first["question_text"] = build_bundled_replace_question_text(first)
            bundled.append(first)
        else:
            for it in group:
                out = dict(it)
                out.pop("targets", None)
                out["question_text"] = build_single_replace_question_text(out)
                bundled.append(out)

    bundled.sort(
        key=lambda x: (
            int(x.get("priority_tier") or TIER_LOW),
            int(x.get("priority_rank") or 50),
            int(
                min(int(t.get("span_start") or -1) for t in (x.get("targets") or [x]))
            ),
        )
    )
    bundled = bundle_shiai_garble_items(bundled)
    bundled.sort(
        key=lambda x: (
            int(x.get("priority_tier") or TIER_LOW),
            int(x.get("priority_rank") or 50),
            int(
                min(int(t.get("span_start") or -1) for t in (x.get("targets") or [x]))
            ),
        )
    )
    for i, entry in enumerate(bundled, start=1):
        entry["review_index"] = i
    return bundled


def prioritize_step3_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach materiality metadata and sort (high impact first)."""
    enriched: list[dict[str, Any]] = []
    for it in items:
        meta = score_step3_materiality(it)
        enriched.append({**it, **meta})
    enriched.sort(
        key=lambda x: (
            int(x.get("priority_tier") or TIER_LOW),
            int(x.get("priority_rank") or 50),
            int(x.get("span_start") or -1),
        )
    )
    return enriched
