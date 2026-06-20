"""Shared helpers for Phase 10 ②③ answers.json template export."""
from __future__ import annotations

import re
from typing import Any


def sentences_around(
    text: str,
    pos: int,
    span_len: int,
    *,
    n_before: int = 2,
    n_after: int = 2,
) -> str:
    """Return surrounding sentences (default: 2 before + current + 2 after)."""
    parts: list[tuple[int, int, str]] = []
    start = 0
    for m in re.finditer(r"[。!?]", text):
        end = m.end()
        chunk = text[start:end].strip()
        if chunk:
            parts.append((start, end, chunk))
        start = end
    if start < len(text):
        tail = text[start:].strip()
        if tail:
            parts.append((start, len(text), tail))

    if not parts:
        return text[max(0, pos - 200) : pos + span_len + 200].strip()

    center = pos + max(span_len // 2, 0)
    idx = 0
    for i, (s, e, _) in enumerate(parts):
        if s <= center < e:
            idx = i
            break
    else:
        idx = max(0, len(parts) - 1)

    lo = max(0, idx - n_before)
    hi = min(len(parts), idx + n_after + 1)
    return "\n\n".join(parts[j][2] for j in range(lo, hi))


def context_for_proposal(
    proposal: dict[str, Any],
    text: str,
    *,
    n_before: int = 2,
    n_after: int = 2,
) -> str:
    span_before = str(proposal.get("span_before") or "")
    start = int(proposal.get("span_start") or -1)
    if start < 0 and span_before:
        start = text.find(span_before)
    if start >= 0 and span_before:
        return sentences_around(
            text, start, len(span_before), n_before=n_before, n_after=n_after
        )
    evidence = str(proposal.get("evidence") or "").strip()
    if evidence:
        return evidence
    return str(proposal.get("reason") or "").strip()


def highlight_anomaly(display: str, word: str) -> str:
    w = str(word or "").strip()
    if not w:
        return display
    if w in display:
        return display.replace(w, f"【{w}】", 1)
    return f"【{display}】" if display else f"【{w}】"


def build_ask_question_text(proposal: dict[str, Any]) -> str:
    span_before = str(proposal.get("span_before") or "").strip()
    hypothesis = str(proposal.get("hypothesis") or "").strip()
    anomaly_word = str(proposal.get("anomaly_word") or "").strip()
    display = highlight_anomaly(span_before or anomaly_word, anomaly_word or span_before[:20])
    if hypothesis:
        return (
            f"「{display}」は「{hypothesis}」では？ "
            "合っていれば「正しい」、違えば正しい語、議事録に不要なら「削除」と返信してください。"
        )
    return (
        f"「{display}」はこの文脈に合いません。"
        "正しい語があれば返信、不要なら「削除」と返信してください。"
    )
