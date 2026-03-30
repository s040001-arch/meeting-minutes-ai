"""
質問1件選定: 「1問で議事録の不確定性をどれだけ減らせるか」を主軸にする。

スコアは impact（種別＋出現）と recoverability（文脈上の軽さ）に加え、
misstatement_risk（誤ったまま残すリスクのヒューリスティック）、
dependency_anchor（他の解釈の前提になりやすいか）、
late_document_bonus（後半＝合意・数値・期限が出やすい位置）を加味する。

「公開情報で解けるか」は使わない。
"""

from __future__ import annotations

import re
from typing import Any

from generate_one_question import TYPE_PRIORITY

# run_question_cycle_once.RISKY_TYPE_PRIORITY と同一（循環 import 回避のためここに定義）
RISKY_TYPE_PRIORITY: dict[str, str] = {
    "proper_noun_candidate": "固有名詞",
    "organization_candidate": "固有名詞",
    "service_candidate": "固有名詞",
    "suspicious_word": "固有名詞",
    "suspicious_number_or_role": "数値",
}

_BASE_IMPACT: dict[str, int] = {
    "organization_candidate": 10,
    "service_candidate": 10,
    "proper_noun_candidate": 8,
    "suspicious_number_or_role": 6,
    "suspicious_word": 5,
    "固有名詞": 7,
    "数値": 5,
    "主語": 5,
}

_BASE_RECOVERABILITY: dict[str, int] = {
    "organization_candidate": 2,
    "service_candidate": 2,
    "proper_noun_candidate": 4,
    "suspicious_number_or_role": 3,
    "数値": 3,
    "主語": 4,
    "固有名詞": 5,
    "suspicious_word": 7,
}

_CONTEXT_MARKERS = (
    "みたいに",
    "みたいな",
    "とか",
    "例えば",
    "例え",
    "なんか",
    "比喩",
    "勢い",
    "雑談",
)

# 誤記のまま残したときの実害が出やすい語（金額・期限・合意系）
_RISK_TERM_PATTERNS: list[tuple[str, int]] = [
    (r"[0-9０-９一二三四五六七八九十百千]+万", 3),
    (r"[0-9０-９]+円", 2),
    (r"(金額|予算|費用|単価|売上げ?|契約|請求|支払|インセンティブ)", 2),
    (r"(期限|締め切り|までに|日まで|来週|来月|四半期|年度内|下期|上期|盆前|応募前)", 2),
    (r"(担当|責任者|承認|合意|決定|タスク|TODO|フォロー)", 2),
]


def _base_impact_for_type(type_raw: str) -> int:
    return _BASE_IMPACT.get(type_raw, 4)


def _occurrence_bonus_and_token(text: str, full_text: str) -> tuple[int, str | None]:
    if not full_text.strip():
        return 0, None
    t = text.strip()
    if not t:
        return 0, None
    tokens = re.findall(r"[A-Za-z]{2,}", t)
    if tokens:
        best_token: str | None = None
        best_count = -1
        for tok in sorted(set(tokens)):
            c = full_text.count(tok)
            if c > best_count or (c == best_count and (best_token is None or tok < best_token)):
                best_count = c
                best_token = tok
        if best_token is None:
            return 0, None
        count = best_count
    else:
        needle = t[:12]
        best_token = needle if needle else None
        count = 0
        if needle:
            start = 0
            while True:
                pos = full_text.find(needle, start)
                if pos == -1:
                    break
                count += 1
                start = pos + max(1, len(needle))
    bonus = min(3, max(0, count - 1))
    return bonus, best_token


def compute_impact(candidate: dict[str, Any], full_text: str) -> int:
    type_raw = str(candidate.get("type", ""))
    base = _base_impact_for_type(type_raw)
    text = str(candidate.get("text", ""))
    bonus, _ = _occurrence_bonus_and_token(text, full_text)
    total = base + bonus
    return min(13, total)


def _base_recoverability_for_type(type_raw: str) -> int:
    return _BASE_RECOVERABILITY.get(type_raw, 5)


def _context_window(full_text: str, candidate_text: str) -> str:
    if not full_text or not candidate_text:
        return ""
    t = candidate_text
    needle = t[:40] if len(t) > 40 else t
    if not needle:
        return ""
    pos = full_text.find(needle)
    if pos == -1:
        return ""
    return full_text[max(0, pos - 80) : pos + len(t) + 80]


def _context_bonus(window: str) -> int:
    if not window:
        return 0
    for m in _CONTEXT_MARKERS:
        if m in window:
            return 2
    return 0


def compute_recoverability(candidate: dict[str, Any], full_text: str) -> int:
    type_raw = str(candidate.get("type", ""))
    base = _base_recoverability_for_type(type_raw)
    bonus = 0
    if full_text.strip():
        window = _context_window(full_text, str(candidate.get("text", "")))
        bonus = _context_bonus(window)
    return min(10, base + bonus)


def compute_misstatement_risk(candidate: dict[str, Any], full_text: str) -> int:
    """
    誤ったまま残すと実行・対外説明・手戻りにつながりやすいか（0〜上限）。
    本文・直近ウィンドウのキーワードと type のみ。公開情報可否は見ない。
    """
    text = str(candidate.get("text", ""))
    type_raw = str(candidate.get("type", ""))
    window = ""
    if full_text.strip():
        window = _context_window(full_text, text)
    combined = f"{text}\n{window}"
    score = 0
    for pat, w in _RISK_TERM_PATTERNS:
        if re.search(pat, combined):
            score += w
    if type_raw in ("数値", "suspicious_number_or_role"):
        score += 2
    if type_raw == "主語":
        score += 2
    if type_raw == "suspicious_word":
        score += 1
    return min(8, score)


def compute_dependency_anchor(type_raw: str) -> int:
    """
    他の文の解釈がこの確定に依存しやすいか（主語・数値・期限系を優先）。
    """
    if type_raw == "主語":
        return 3
    if type_raw in ("数値", "suspicious_number_or_role"):
        return 3
    if type_raw in (
        "organization_candidate",
        "service_candidate",
        "proper_noun_candidate",
    ):
        return 1
    if type_raw in ("固有名詞",):
        return 1
    return 0


def late_document_anchor_bonus(candidate: dict[str, Any], full_text: str) -> int:
    """後半（合意・数値・次アクションが出やすい位置）にあるか。最大 +1。"""
    text = str(candidate.get("text", "")).strip()
    if not full_text.strip() or len(text) < 4:
        return 0
    needle = text[: min(32, len(text))]
    pos = full_text.find(needle)
    if pos < 0:
        return 0
    ratio = pos / max(len(full_text), 1)
    if ratio >= 0.52:
        return 1
    return 0


def compute_tiebreak_key(idx: int, item: dict[str, Any]) -> tuple[int, int, int]:
    raw_type = str(item.get("type", ""))
    mapped = RISKY_TYPE_PRIORITY.get(raw_type, raw_type)
    risky_band = 0 if raw_type in RISKY_TYPE_PRIORITY else 1
    base_priority = TYPE_PRIORITY.get(mapped, 999)
    return (risky_band, base_priority, idx)


def deduplicate_unknown_points_by_type_text(
    unknown_points: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    (type, text) が同一の行を1件に集約する。
    代表行は「元配列で最も若い idx」の要素を浅いコピーし、
    _dedupe_source_indexes に昇順の元 idx 一覧を付与する。
    出力順は、元配列を先頭から走査したときのキー初出順。
    """
    key_to_indices: dict[tuple[str, str], list[int]] = {}
    for idx, item in enumerate(unknown_points):
        key = (
            str(item.get("type", "")).strip(),
            str(item.get("text", "")).strip(),
        )
        key_to_indices.setdefault(key, []).append(idx)

    key_order: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in unknown_points:
        key = (
            str(item.get("type", "")).strip(),
            str(item.get("text", "")).strip(),
        )
        if key not in seen:
            seen.add(key)
            key_order.append(key)

    deduped: list[dict[str, Any]] = []
    for key in key_order:
        indices = sorted(key_to_indices[key])
        rep_idx = indices[0]
        rep = dict(unknown_points[rep_idx])
        rep["_dedupe_source_indexes"] = indices
        deduped.append(rep)

    meta = {
        "deduplicated": len(unknown_points) > len(deduped),
        "unknown_points_count_before": len(unknown_points),
        "unknown_points_count_after": len(deduped),
    }
    return deduped, meta


def compute_selection_value(
    item: dict[str, Any], full_text: str
) -> tuple[int, int, int, int, int, int]:
    """
    Returns:
        value, impact, recoverability, misstatement_risk, dependency_anchor, late_document_bonus
    """
    imp = compute_impact(item, full_text)
    rec = compute_recoverability(item, full_text)
    risk = compute_misstatement_risk(item, full_text)
    dep = compute_dependency_anchor(str(item.get("type", "")))
    late = late_document_anchor_bonus(item, full_text)
    val = imp - rec + risk + dep + late
    return val, imp, rec, risk, dep, late


def select_one_unknown_value_based(
    unknown_points: list[dict[str, Any]],
    full_text: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not unknown_points:
        raise ValueError("unknown points is empty.")

    deduped, dedupe_meta = deduplicate_unknown_points_by_type_text(unknown_points)

    scored: list[
        tuple[int, dict[str, Any], int, int, int, int, int, int]
    ] = []
    for idx, item in enumerate(deduped):
        val, imp, rec, risk, dep, late = compute_selection_value(item, full_text)
        scored.append((idx, item, imp, rec, val, risk, dep, late))

    max_value = max(t[4] for t in scored)
    top = [t for t in scored if t[4] == max_value]
    tiebreak_used = len(top) > 1

    best = min(
        top,
        key=lambda t: compute_tiebreak_key(t[0], t[1]),
    )
    dedup_idx, item, imp, rec, val, risk, dep, late = best

    source_indexes = list(item.get("_dedupe_source_indexes") or [dedup_idx])
    canonical_original_idx = min(source_indexes)

    item["_impact"] = imp
    item["_recoverability"] = rec
    item["_misstatement_risk"] = risk
    item["_dependency_anchor"] = dep
    item["_late_document_bonus"] = late
    item["_value"] = val

    raw_type = str(item.get("type", ""))
    mapped = RISKY_TYPE_PRIORITY.get(raw_type, raw_type)
    risky_band = 0 if raw_type in RISKY_TYPE_PRIORITY else 1
    base_priority = TYPE_PRIORITY.get(mapped, 999)

    audit: dict[str, Any] = {
        "index_in_unknown_points": canonical_original_idx,
        "index_in_deduped_points": dedup_idx,
        "unknown_points_count": dedupe_meta["unknown_points_count_before"],
        "unknown_points_count_after_dedup": dedupe_meta["unknown_points_count_after"],
        "type_raw": raw_type,
        "risky_band": risky_band,
        "type_priority_rank": base_priority,
        "selection_mode": "value_based_uncertainty_v2",
        "value_formula": "impact - recoverability + misstatement_risk + dependency_anchor + late_document_bonus",
        "impact": imp,
        "recoverability": rec,
        "misstatement_risk": risk,
        "dependency_anchor": dep,
        "late_document_bonus": late,
        "value": val,
        "tiebreak_used": tiebreak_used,
        "deduplicated": dedupe_meta["deduplicated"],
        "duplicate_count": len(source_indexes),
        "source_indexes": source_indexes,
    }

    return item, audit


def pop_value_fields(selected: dict[str, Any]) -> None:
    selected.pop("_impact", None)
    selected.pop("_recoverability", None)
    selected.pop("_misstatement_risk", None)
    selected.pop("_dependency_anchor", None)
    selected.pop("_late_document_bonus", None)
    selected.pop("_value", None)
    selected.pop("_dedupe_source_indexes", None)


def format_top_candidates_debug(
    unknown_points: list[dict[str, Any]],
    full_text: str,
    limit: int = 8,
) -> str:
    rows: list[tuple[int, int, int, int, int, int, int, str]] = []
    for idx, item in enumerate(unknown_points):
        val, imp, rec, risk, dep, late = compute_selection_value(item, full_text)
        rows.append((val, imp, rec, risk, dep, late, idx, str(item.get("type", ""))))
    rows.sort(key=lambda x: (-x[0], x[6]))
    lines = [
        "selection_debug_top_candidates=",
        "  (value = impact - recoverability + misstatement_risk + dependency_anchor + late_document_bonus)",
    ]
    for val, imp, rec, risk, dep, late, idx, t in rows[:limit]:
        lines.append(
            f"  idx={idx} value={val} impact={imp} rec={rec} risk={risk} dep={dep} "
            f"late={late} type_raw={t}"
        )
    return "\n".join(lines)
