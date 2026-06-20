"""Supplemental ④ (auto_delete) proposals for filler / repetition / stutter noise."""
from __future__ import annotations

import re
from typing import Any

from edit_proposal_schema import (
    FACT_FILLER_GARBLE,
    VERDICT_AUTO_DELETE,
    new_proposal_id,
)
from fact_classify import (
    classify_fact_class,
    is_substantive_datetime,
    is_substantive_numeric,
    looks_like_garble_span,
)

_FILLER_STANDALONE_RE = re.compile(
    r"(?:えっと|えーと|うーん|そのー|ええと)"
)
_SOFT_FILLER_RE = re.compile(r"(?:あの|まあ|なんか)")
_EXPLANATORY_AFTER_RE = re.compile(
    r"^(こう|何|その|これ|あれ|え、|いう|すご|めちゃ|本当|一応)"
)
_SOCIAL_DUPLICATE_PHRASES = (
    "お願いします",
    "お疲れ様",
    "ありがとうございます",
    "失礼します",
    "失礼いたします",
    "よろしくお願いします",
)


def _occupied_ranges(proposals: list[dict[str, Any]]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for p in proposals:
        start = int(p.get("span_start") or -1)
        span = str(p.get("span_before") or "")
        if start < 0 or not span:
            continue
        ranges.append((start, start + len(span)))
    return ranges


def _overlaps_existing(start: int, end: int, occupied: list[tuple[int, int]]) -> bool:
    for a, b in occupied:
        if start < b and end > a:
            return True
    return False


def _is_safe_filler_delete(
    span: str,
    *,
    meeting_profile: dict[str, Any] | None,
    reason: str = "",
) -> bool:
    s = str(span or "").strip()
    if not s or len(s) > 48:
        return False
    if is_substantive_numeric(s) or is_substantive_datetime(s):
        return False
    fc, _ = classify_fact_class(
        span_before=s,
        span_after="",
        llm_fact_class=FACT_FILLER_GARBLE,
        llm_verdict=VERDICT_AUTO_DELETE,
        meeting_profile=meeting_profile,
    )
    if fc not in {FACT_FILLER_GARBLE, "uncertain", "lexical_fluency"}:
        return False
    if looks_like_garble_span(s) or _FILLER_STANDALONE_RE.fullmatch(s):
        return True
    if len(s) <= 24 and any(
        k in reason for k in ("重複", "吃音", "挨拶", "フィラー", "言い直し")
    ):
        return fc != "proper_noun"
    return False


def _candidate_key(start: int, span: str) -> tuple[int, str]:
    return (start, span)


def _add_candidate(
    candidates: dict[tuple[int, str], dict[str, Any]],
    *,
    text: str,
    start: int,
    span: str,
    reason: str,
    anomaly_word: str,
    meeting_profile: dict[str, Any] | None,
    occupied: list[tuple[int, int]],
) -> None:
    span = str(span or "")
    if not span or start < 0 or text[start : start + len(span)] != span:
        return
    end = start + len(span)
    if _overlaps_existing(start, end, occupied):
        return
    if not _is_safe_filler_delete(span, meeting_profile=meeting_profile, reason=reason):
        return
    key = _candidate_key(start, span)
    if key in candidates:
        return
    candidates[key] = {
        "span_before": span,
        "span_start": start,
        "reason": reason[:50],
        "anomaly_word": (anomaly_word or span[:20]).strip(),
    }


def _find_social_duplicate_phrases(
    text: str,
    *,
    meeting_profile: dict[str, Any] | None,
    occupied: list[tuple[int, int]],
    candidates: dict[tuple[int, str], dict[str, Any]],
) -> None:
    """挨拶・相槌の準完全重複（お願いします。。。お願いします 等）。"""
    for phrase in _SOCIAL_DUPLICATE_PHRASES:
        pattern = re.compile(
            rf"({re.escape(phrase)})([、。.…\s]{{1,12}}\1)"
        )
        for m in pattern.finditer(text):
            delete_span = m.group(2)
            start = m.start(2)
            _add_candidate(
                candidates,
                text=text,
                start=start,
                span=delete_span,
                reason="挨拶・相槌の重複残骸",
                anomaly_word=phrase,
                meeting_profile=meeting_profile,
                occupied=occupied,
            )


def _find_tandem_word_duplicates(
    text: str,
    *,
    meeting_profile: dict[str, Any] | None,
    occupied: list[tuple[int, int]],
    candidates: dict[tuple[int, str], dict[str, Any]],
) -> None:
    """いきなりいきなり 等の連続重複語（4字以上のみ）。"""
    pattern = re.compile(r"([\u3040-\u9fff\u30a0-\u30ff]{4,12}?)\1")
    for m in pattern.finditer(text):
        word = m.group(1)
        end = m.end()
        if end < len(text) and text[end] in "さでは":
            continue
        if is_substantive_numeric(word) or is_substantive_datetime(word):
            continue
        start = m.start(1) + len(word)
        _add_candidate(
            candidates,
            text=text,
            start=start,
            span=word,
            reason="言い直し・吃音の重複語",
            anomaly_word=word,
            meeting_profile=meeting_profile,
            occupied=occupied,
        )


def _is_punct(ch: str) -> bool:
    return ch in "、。.… \t\n" or not ch


def _find_redundant_fillers(
    text: str,
    *,
    meeting_profile: dict[str, Any] | None,
    occupied: list[tuple[int, int]],
    candidates: dict[tuple[int, str], dict[str, Any]],
) -> None:
    """意味を足さないフィラー単独。あの/なんかは説明語に続く場合は残す。"""
    for m in _FILLER_STANDALONE_RE.finditer(text):
        token = m.group(0)
        start = m.start()
        end = m.end()
        before = text[start - 1] if start > 0 else ""
        after = text[end : end + 8]
        embedded_ok = (
            before
            and "\u3040" <= before <= "\u9fff"
            and after
            and (
                "\u30a0" <= after[0] <= "\u30ff"
                or "\u4e00" <= after[0] <= "\u9fff"
                or after[0].isascii()
            )
        )
        if before and not _is_punct(before) and not embedded_ok:
            continue
        if _EXPLANATORY_AFTER_RE.match(after):
            continue
        if token == "あの" and re.match(r"^[\u4e00-\u9fff\u30a0-\u30ffA-Za-z]{2,}", after):
            continue
        _add_candidate(
            candidates,
            text=text,
            start=start,
            span=token,
            reason="意味を足さないフィラー",
            anomaly_word=token,
            meeting_profile=meeting_profile,
            occupied=occupied,
        )

    for m in _SOFT_FILLER_RE.finditer(text):
        token = m.group(0)
        start = m.start()
        end = m.end()
        before = text[start - 1] if start > 0 else ""
        after_ch = text[end] if end < len(text) else ""
        after = text[end : end + 8]
        if not _is_punct(before) or not _is_punct(after_ch):
            continue
        if _EXPLANATORY_AFTER_RE.match(after):
            continue
        if token == "あの" and re.match(r"^[\u4e00-\u9fff\u30a0-\u30ffA-Za-z]{2,}", after):
            continue
        _add_candidate(
            candidates,
            text=text,
            start=start,
            span=token,
            reason="意味を足さないフィラー",
            anomaly_word=token,
            meeting_profile=meeting_profile,
            occupied=occupied,
        )


def expand_filler_garble_proposals(
    text: str,
    existing: list[dict[str, Any]],
    *,
    meeting_profile: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Add supplemental ④ proposals without altering existing ①②③ routing."""
    if not text.strip():
        return []
    occupied = _occupied_ranges(existing)
    candidates: dict[tuple[int, str], dict[str, Any]] = {}

    _find_social_duplicate_phrases(
        text, meeting_profile=meeting_profile, occupied=occupied, candidates=candidates
    )
    _find_tandem_word_duplicates(
        text, meeting_profile=meeting_profile, occupied=occupied, candidates=candidates
    )
    _find_redundant_fillers(
        text, meeting_profile=meeting_profile, occupied=occupied, candidates=candidates
    )

    supplemental: list[dict[str, Any]] = []
    for cand in sorted(candidates.values(), key=lambda c: int(c["span_start"])):
        start = int(cand["span_start"])
        span = str(cand["span_before"])
        supplemental.append(
            {
                "proposal_id": new_proposal_id(),
                "verdict": VERDICT_AUTO_DELETE,
                "fact_class": FACT_FILLER_GARBLE,
                "span_before": span,
                "garble_fragment": span,
                "span_after": "",
                "hypothesis": "",
                "evidence": text[max(0, start - 40) : start + len(span) + 40],
                "importance": "読みやすさのためのノイズ削除",
                "reason": cand["reason"],
                "anomaly_word": cand["anomaly_word"],
                "span_start": start,
                "supplemental_filler_expand": True,
            }
        )
        occupied.append((start, start + len(span)))
    return supplemental
