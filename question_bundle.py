"""Safe-tier grouping for Phase 10 step-2 ② (ask_with_candidate) questions."""
from __future__ import annotations

import re
import uuid
from typing import Any

from edit_proposal_schema import VERDICT_ASK_WITH_CANDIDATE, normalize_verdict
from recognition_batch import _is_delete_answer, _is_keep_answer

BUNDLE_KIND_REPLACE = "replace"
BUNDLE_KIND_DELETE = "delete"


def normalize_hypothesis(h: str) -> str:
    return re.sub(r"\s+", "", str(h or "").strip())


def effective_landing(answer: str, hypothesis: str) -> str:
    """Landing string used to decide if two ② items share the same correction."""
    ans = str(answer or "").strip()
    hyp = str(hypothesis or "").strip()
    if _is_delete_answer(ans):
        return "__DELETE__"
    if _is_keep_answer(ans, hyp) or ans in ("正しい",):
        return normalize_hypothesis(hyp)
    return normalize_hypothesis(ans)


def bundle_kind_for_item(item: dict[str, Any]) -> str:
    """Replace and delete bundles are never mixed."""
    ans = str(item.get("answer_text") or "").strip()
    if ans and _is_delete_answer(ans):
        return BUNDLE_KIND_DELETE
    return BUNDLE_KIND_REPLACE


def anomalies_suggest_single_landing(hypothesis: str, anomalies: list[str]) -> bool:
    """Pre-answer heuristic: exclude groups like 季央×2 (TOKIO + 定着さん)."""
    words = [str(a or "").strip() for a in anomalies if str(a or "").strip()]
    if len(words) < 2:
        return True
    has_san = [w.endswith("さん") for w in words]
    if len(set(has_san)) > 1:
        return False
    hyp = str(hypothesis or "").strip()
    if any(has_san) and not hyp.endswith("さん"):
        if not all(has_san):
            return False
    return True


def _can_safe_merge_group(items: list[dict[str, Any]]) -> bool:
    if len(items) < 2:
        return False
    kinds = {bundle_kind_for_item(it) for it in items}
    if len(kinds) != 1:
        return False
    hyps = {normalize_hypothesis(str(it.get("hypothesis") or "")) for it in items}
    hyps.discard("")
    if len(hyps) != 1:
        return False
    hyp_raw = str(items[0].get("hypothesis") or "").strip()
    anomalies = [str(it.get("anomaly_word") or "") for it in items]
    if not anomalies_suggest_single_landing(hyp_raw, anomalies):
        return False
    answered = [str(it.get("answer_text") or "").strip() for it in items]
    if any(answered):
        landings = {effective_landing(a, hyp_raw) for a in answered}
        if len(landings) != 1:
            return False
    return True


def proposal_to_target(item: dict[str, Any]) -> dict[str, Any]:
    """Minimal target payload for apply expansion."""
    return {
        "proposal_id": item.get("proposal_id"),
        "anomaly_word": item.get("anomaly_word"),
        "span_before": item.get("span_before"),
        "span_start": item.get("span_start"),
        "hypothesis": item.get("hypothesis"),
        "fact_class": item.get("fact_class"),
        "context": item.get("context"),
        "reason": item.get("reason"),
        "importance": item.get("importance"),
        "selected_unknown": item.get("selected_unknown"),
        "review_index": item.get("review_index"),
    }


def _highlight_word(display: str, word: str) -> str:
    w = str(word or "").strip()
    if not w:
        return display
    if w in display:
        return display.replace(w, f"【{w}】", 1)
    return f"【{display}】" if display else f"【{w}】"


def build_single_replace_question_text(item: dict[str, Any]) -> str:
    span_before = str(item.get("span_before") or "").strip()
    hypothesis = str(item.get("hypothesis") or "").strip()
    anomaly_word = str(item.get("anomaly_word") or "").strip()
    display = _highlight_word(span_before or anomaly_word, anomaly_word or span_before[:20])
    if hypothesis:
        return (
            f"「{display}」は「{hypothesis}」では？ "
            "合っていれば「正しい」、違えば正しい語、議事録に不要なら「削除」と返信してください。"
        )
    return (
        f"「{display}」はこの文脈に合いません。"
        "正しい語があれば返信、不要なら「削除」と返信してください。"
    )


def build_bundled_replace_question_text(bundle: dict[str, Any]) -> str:
    targets = list(bundle.get("targets") or [])
    hypothesis = str(bundle.get("hypothesis") or "").strip()
    lines: list[str] = []
    for i, t in enumerate(targets, 1):
        span_before = str(t.get("span_before") or "").strip()
        anomaly_word = str(t.get("anomaly_word") or "").strip()
        display = _highlight_word(span_before or anomaly_word, anomaly_word or span_before[:20])
        lines.append(f"{i}.「{display}」")
    joined = "\n".join(lines)
    if hypothesis:
        return (
            f"以下の箇所はいずれも「{hypothesis}」では？\n{joined}\n"
            "すべて合っていれば「正しい」、違えば正しい語、議事録に不要なら「削除」と返信してください。"
        )
    return (
        f"以下の箇所はこの文脈に合いません。\n{joined}\n"
        "正しい語があれば返信、不要なら「削除」と返信してください。"
    )


def bundle_safe_answer_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group ② items into safe bundles; unmergeable items stay single (no targets)."""
    replace_items = [
        it
        for it in items
        if normalize_verdict(
            (it.get("selected_unknown") or {}).get("verdict") or VERDICT_ASK_WITH_CANDIDATE
        )
        == VERDICT_ASK_WITH_CANDIDATE
        and bundle_kind_for_item(it) == BUNDLE_KIND_REPLACE
    ]
    delete_items = [it for it in items if bundle_kind_for_item(it) == BUNDLE_KIND_DELETE]
    other_items = [it for it in items if it not in replace_items and it not in delete_items]

    by_hyp: dict[str, list[dict[str, Any]]] = {}
    for it in replace_items:
        key = normalize_hypothesis(str(it.get("hypothesis") or ""))
        if not key:
            other_items.append(it)
            continue
        by_hyp.setdefault(key, []).append(it)

    bundled: list[dict[str, Any]] = []
    singles: list[dict[str, Any]] = []

    for group in by_hyp.values():
        if _can_safe_merge_group(group):
            first = dict(group[0])
            targets = [proposal_to_target(g) for g in group]
            first["question_id"] = str(first.get("question_id") or uuid.uuid4())
            first["bundle_kind"] = BUNDLE_KIND_REPLACE
            first["targets"] = targets
            first["question_text"] = build_bundled_replace_question_text(first)
            bundled.append(first)
        else:
            singles.extend(group)

    for it in singles:
        out = dict(it)
        out.pop("targets", None)
        out["question_text"] = build_single_replace_question_text(out)
        bundled.append(out)

    for it in delete_items:
        out = dict(it)
        out.pop("targets", None)
        bundled.append(out)

    for it in other_items:
        out = dict(it)
        out.pop("targets", None)
        if not str(out.get("question_text") or "").strip():
            out["question_text"] = build_single_replace_question_text(out)
        bundled.append(out)

    bundled.sort(
        key=lambda x: min(
            int(t.get("span_start") or -1) for t in (x.get("targets") or [x])
        )
    )
    for i, entry in enumerate(bundled, start=1):
        entry["review_index"] = i
    return bundled


def expand_answer_items_for_apply(answers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fan out bundled answers to per-span items for apply (backward compatible)."""
    expanded: list[dict[str, Any]] = []
    for item in answers:
        ans = str(item.get("answer_text") or "").strip()
        if not ans:
            continue
        targets = item.get("targets")
        if not isinstance(targets, list) or not targets:
            expanded.append(item)
            continue
        qid = item.get("question_id")
        bundle_kind = item.get("bundle_kind")
        for t in targets:
            if not isinstance(t, dict):
                continue
            expanded.append(
                {
                    **t,
                    "answer_text": ans,
                    "question_id": qid,
                    "bundle_kind": bundle_kind,
                    "selected_unknown": t.get("selected_unknown") or item.get("selected_unknown"),
                }
            )
    return expanded
