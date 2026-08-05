"""Span-local text correction utilities (Phase 1: coherence_review auto_fix).

Replaces a single standalone word occurrence at a known offset instead of
global str.replace across the full transcript.
"""
from __future__ import annotations

from typing import Any

from recognition_batch import find_standalone_word

DEFAULT_SPAN_CONTEXT_CHARS = 30


def resolve_word_position(
    text: str,
    word: str,
    *,
    hint_pos: int = -1,
    context: str = "",
) -> int:
    """Return the start index of a standalone ``word`` occurrence, or -1."""
    w = str(word or "").strip()
    if not w or not text:
        return -1
    hint = int(hint_pos) if isinstance(hint_pos, int) and hint_pos >= 0 else -1
    if hint < 0 and context:
        hint = text.find(str(context)[:20])
    return find_standalone_word(text, w, hint_pos=hint)


def extract_span_text(
    text: str,
    start: int,
    word_len: int,
    *,
    context_chars: int = DEFAULT_SPAN_CONTEXT_CHARS,
) -> str:
    """Extract verbatim span_text: word plus surrounding context."""
    if start < 0 or word_len <= 0 or not text:
        return ""
    end = start + word_len
    clip_start = max(0, start - context_chars)
    clip_end = min(len(text), end + context_chars)
    snippet = text[clip_start:clip_end]
    if clip_start > 0:
        snippet = "…" + snippet
    if clip_end < len(text):
        snippet = snippet + "…"
    return snippet


def build_span_fields(
    text: str,
    *,
    word: str,
    estimated: str,
    hint_pos: int = -1,
    context: str = "",
    llm_span_text: str = "",
) -> dict[str, Any]:
    """Derive span_* fields for an anomaly (LLM value + position backfill)."""
    start = resolve_word_position(text, word, hint_pos=hint_pos, context=context)
    word_len = len(word)
    span_text = str(llm_span_text or "").strip()
    if not span_text and start >= 0:
        span_text = extract_span_text(text, start, word_len)
    span_end = start + word_len if start >= 0 else -1
    return {
        "span_start": start,
        "span_end": span_end,
        "span_text": span_text,
        "span_corrected": str(estimated or "").strip(),
    }


def apply_span_word_replacement(
    text: str,
    *,
    start: int,
    wrong: str,
    right: str,
) -> tuple[str, bool]:
    """Replace ``wrong`` with ``right`` only at ``start`` (single occurrence)."""
    if start < 0 or not wrong or not right or wrong == right:
        return text, False
    if text[start : start + len(wrong)] != wrong:
        return text, False
    return text[:start] + right + text[start + len(wrong) :], True


def _is_word_level_replacement(wrong: str, replacement: str) -> bool:
    """``replacement`` が単語 ``wrong`` の素直な置換先として妥当か。

    2026-08-05 バグ対策: reconstruct 系は span_corrected に「文全体の修正版」
    を入れることがある。それを単語位置の置換に使うと、周辺の文が二重化する
    （楽天ジョブで「プレサナ」→修正文まるごと、が発生）。文らしきもの・
    極端に長いものは単語置換に使わない。
    """
    if not replacement:
        return False
    if "。" in replacement or "\n" in replacement:
        return False
    return len(replacement) <= max(len(wrong) * 3, len(wrong) + 10)


def apply_span_correction_from_anomaly(
    text: str,
    anomaly: dict[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    """Apply one auto_fixable anomaly at its span position.

    優先順: (1) 単語レベル置換（wrong→妥当な単語級の修正先）、
    (2) span_text 全体→span_corrected のスパン置換（span_corrected が
    文全体の修正版だった場合の安全な適用方法）。どちらも成立しなければ
    何もしない（呼び出し側で未解決として扱われる）。
    """
    if not anomaly.get("auto_fixable"):
        return text, None
    wrong = str(anomaly.get("anomaly_word") or "").strip()
    estimated = str(anomaly.get("estimated_correction") or "").strip()
    span_corrected = str(anomaly.get("span_corrected") or "").strip()
    span_text = str(anomaly.get("span_text") or "").strip()

    right = ""
    if _is_word_level_replacement(wrong, span_corrected):
        right = span_corrected
    elif _is_word_level_replacement(wrong, estimated):
        right = estimated

    if wrong and right and right != wrong:
        start = anomaly.get("span_start", -1)
        try:
            start = int(start)
        except (TypeError, ValueError):
            start = -1
        if start < 0:
            start = resolve_word_position(
                text,
                wrong,
                hint_pos=int(anomaly.get("context_position_in_transcript") or -1),
                context=str(anomaly.get("context") or ""),
            )
        new_text, changed = apply_span_word_replacement(
            text, start=start, wrong=wrong, right=right
        )
        if changed:
            return new_text, {
                "anomaly_id": anomaly.get("anomaly_id"),
                "before": wrong,
                "after": right,
                "action": "correct",
                "confidence": anomaly.get("confidence"),
                "reason": anomaly.get("reason", ""),
                "span_start": start,
                "span_end": start + len(wrong),
                "span_text": span_text,
                "span_corrected": right,
                "occurrences_replaced": 1,
            }

    # 単語置換ができない場合: span_corrected が文全体の修正版なら、
    # 対応する span_text 全体を一意に置き換える。
    if (
        span_text
        and span_corrected
        and span_corrected != span_text
        and text.count(span_text) == 1
    ):
        new_text = text.replace(span_text, span_corrected, 1)
        start = text.find(span_text)
        return new_text, {
            "anomaly_id": anomaly.get("anomaly_id"),
            "before": span_text,
            "after": span_corrected,
            "action": "correct",
            "mode": "span_replace",
            "confidence": anomaly.get("confidence"),
            "reason": anomaly.get("reason", ""),
            "span_start": start,
            "span_end": start + len(span_text),
            "span_text": span_text,
            "span_corrected": span_corrected,
            "occurrences_replaced": 1,
        }
    return text, None


def apply_span_corrections_batch(
    text: str,
    anomalies: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Apply all auto_fixable anomalies; higher offsets first to keep indices stable."""
    fixable = [a for a in anomalies if a.get("auto_fixable")]
    ranked: list[tuple[int, dict[str, Any]]] = []
    for an in fixable:
        try:
            pos = int(an.get("span_start", an.get("context_position_in_transcript", -1)))
        except (TypeError, ValueError):
            pos = -1
        ranked.append((pos, an))
    ranked.sort(key=lambda x: x[0], reverse=True)

    out = text
    applied: list[dict[str, Any]] = []
    for _pos, an in ranked:
        out, entry = apply_span_correction_from_anomaly(out, an)
        if entry:
            applied.append(entry)
    applied.reverse()
    return out, applied
