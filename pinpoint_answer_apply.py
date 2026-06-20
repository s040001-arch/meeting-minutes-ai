"""Pinpoint apply for contextual_editor ②③ LINE answers (no LLM incorporate)."""
from __future__ import annotations

import re
from typing import Any

from edit_proposal_schema import VERDICT_AUTO_DELETE, align_proposal_spans_in_text
from question_bundle import expand_answer_items_for_apply
from recognition_batch import _is_delete_answer, _is_keep_answer


def _collapse_ws(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip())


def _find_span_exact(text: str, span_before: str, hint: int) -> int:
    sb = str(span_before or "").strip()
    if not sb:
        return -1
    if 0 <= hint < len(text) and text[hint : hint + len(sb)] == sb:
        return hint
    pos = 0
    hits: list[int] = []
    while True:
        i = text.find(sb, pos)
        if i < 0:
            break
        hits.append(i)
        pos = i + 1
    if not hits:
        return -1
    if hint >= 0:
        return min(hits, key=lambda i: abs(i - hint))
    return hits[0]


def _find_span_fuzzy(text: str, span_before: str, hint: int) -> tuple[int, str]:
    """Return (start, matched_span) using exact then whitespace-collapsed search."""
    sb = str(span_before or "").strip()
    if not sb:
        return -1, sb
    exact = _find_span_exact(text, sb, hint)
    if exact >= 0:
        return exact, sb
    norm_sb = _collapse_ws(sb)
    if not norm_sb:
        return -1, sb
    chunks: list[tuple[int, int, str]] = []
    i = 0
    while i < len(text):
        if text[i].isspace():
            j = i
            while j < len(text) and text[j].isspace():
                j += 1
            chunks.append((i, j, " "))
            i = j
            continue
        j = i
        while j < len(text) and not text[j].isspace():
            j += 1
        chunks.append((i, j, text[i:j]))
        i = j
    collapsed = "".join(c[2] for c in chunks)
    pos = 0
    hits: list[int] = []
    while True:
        k = collapsed.find(norm_sb, pos)
        if k < 0:
            break
        hits.append(k)
        pos = k + 1
    if not hits:
        return -1, sb
    pick = min(hits, key=lambda k: abs(k - hint)) if hint >= 0 else hits[0]
    ci = 0
    start = -1
    for a, b, piece in chunks:
        if ci <= pick < ci + len(piece):
            start = a + (pick - ci)
            remain = len(norm_sb) - (len(piece) - (pick - ci))
            end = b
            ci = pick + len(piece) - (pick - ci)
            idx = chunks.index((a, b, piece)) + 1
            while remain > 0 and idx < len(chunks):
                a2, b2, piece2 = chunks[idx]
                if piece2 == " ":
                    end = b2
                    idx += 1
                    continue
                take = min(remain, len(piece2))
                end = a2 + take
                remain -= take
                idx += 1
            break
        ci += len(piece)
    if start < 0:
        return -1, sb
    return start, text[start:end]


def resolve_span(text: str, item: dict[str, Any]) -> tuple[int, str]:
    hint = int(item.get("span_start") or -1)
    sb = str(item.get("span_before") or "").strip()
    su = item.get("selected_unknown") or {}
    aligned = align_proposal_spans_in_text(
        text,
        {
            "span_before": sb,
            "span_start": hint,
            "anomaly_word": item.get("anomaly_word"),
            "evidence": su.get("evidence") or item.get("context") or "",
            "verdict": "ask_with_candidate",
        },
    )
    sb2 = str(aligned.get("span_before") or sb).strip()
    start, matched = _find_span_fuzzy(text, sb2, hint)
    if start >= 0:
        return start, matched
    aw = str(item.get("anomaly_word") or "").strip()
    if aw:
        i = text.find(aw, max(0, hint - 200) if hint >= 0 else 0)
        if i < 0:
            i = text.find(aw)
        if i >= 0:
            return i, aw
    return -1, sb2


def _dup_tail(span: str, answer: str) -> bool:
    if not answer or answer not in span:
        return False
    return span.count(answer) > 1 or (span.endswith(answer) and len(span) > len(answer) + 3)


def _map_collapsed_substring(haystack: str, needle: str) -> tuple[int, int] | None:
    if not needle:
        return None
    if needle in haystack:
        i = haystack.find(needle)
        return i, i + len(needle)
    norm_needle = _collapse_ws(needle)
    if not norm_needle:
        return None
    chunks: list[tuple[int, int, str]] = []
    i = 0
    while i < len(haystack):
        if haystack[i].isspace():
            j = i
            while j < len(haystack) and haystack[j].isspace():
                j += 1
            chunks.append((i, j, " "))
            i = j
            continue
        j = i
        while j < len(haystack) and not haystack[j].isspace():
            j += 1
        chunks.append((i, j, haystack[i:j]))
        i = j
    collapsed = "".join(c[2] for c in chunks)
    k = collapsed.find(norm_needle)
    if k < 0:
        return None
    ci = 0
    start = -1
    for a, b, piece in chunks:
        if ci <= k < ci + len(piece):
            start = a + (k - ci)
            remain = len(norm_needle) - (len(piece) - (k - ci))
            end = b
            ci = k + len(piece) - (k - ci)
            idx = chunks.index((a, b, piece)) + 1
            while remain > 0 and idx < len(chunks):
                a2, b2, piece2 = chunks[idx]
                if piece2 == " ":
                    end = b2
                    idx += 1
                    continue
                take = min(remain, len(piece2))
                end = a2 + take
                remain -= take
                idx += 1
            break
        ci += len(piece)
    if start < 0:
        return None
    return start, end


def _find_anomaly_in_span(
    span: str,
    anomaly: str,
    *,
    hint_abs: int,
    span_start: int,
) -> tuple[int, int] | None:
    aw = str(anomaly or "").strip()
    if not aw:
        return None
    hits: list[int] = []
    pos = 0
    while True:
        i = span.find(aw, pos)
        if i < 0:
            break
        hits.append(i)
        pos = i + 1
    if len(hits) == 1:
        i = hits[0]
        return i, i + len(aw)
    if len(hits) > 1:
        rel_hint = max(0, hint_abs - span_start)
        i = min(hits, key=lambda h: abs(h - rel_hint))
        return i, i + len(aw)
    mapped = _map_collapsed_substring(span, aw)
    if mapped:
        return mapped
    return None


def replacement_word(
    span: str,
    anomaly: str,
    answer: str,
    hypothesis: str,
    *,
    matched_text: str,
) -> str:
    """Derive the text that substitutes anomaly_word inside span (never whole span)."""
    aw = str(anomaly or "").strip()
    hyp = str(hypothesis or "").strip()
    ans = str(answer or "").strip()
    if _is_keep_answer(ans, aw) or ans in ("正しい",):
        return hyp or matched_text
    if not aw:
        return ans
    prefix_end = span.find(matched_text)
    if prefix_end < 0:
        return ans
    prefix = span[:prefix_end]
    suffix = span[prefix_end + len(matched_text) :]
    if suffix and ans.endswith(suffix) and len(ans) > len(suffix):
        if prefix and ans.startswith(prefix):
            return ans[len(prefix) : len(ans) - len(suffix)]
        return ans[: len(ans) - len(suffix)]
    trial = span[:prefix_end] + ans + suffix
    if trial != span and not _dup_tail(trial, ans):
        return ans
    return ans


def resolve_span_after(
    item: dict[str, Any],
    answer: str,
    *,
    hint_abs: int,
    span_start: int,
) -> tuple[str | None, str, dict[str, Any]]:
    """Return (span_after, mode, meta). span_after None => delete entire span."""
    sb = str(item.get("span_before") or "").strip()
    aw = str(item.get("anomaly_word") or "").strip()
    hyp = str(item.get("hypothesis") or "").strip()
    meta: dict[str, Any] = {"apply_mode": "unknown"}

    if _is_delete_answer(answer) or answer.strip() == "削除":
        meta["apply_mode"] = "delete_span"
        return None, "delete_span", meta

    loc = _find_anomaly_in_span(sb, aw, hint_abs=hint_abs, span_start=span_start)
    if loc is None:
        raise ValueError(f"anomaly_word not found in span: {aw!r}")

    i, j = loc
    matched = sb[i:j]
    repl = replacement_word(sb, aw, answer, hyp, matched_text=matched)
    span_after = sb[:i] + repl + sb[j:]
    if span_after == sb:
        meta["apply_mode"] = "anomaly_keep_unchanged"
        return sb, "anomaly_keep_unchanged", meta
    meta["apply_mode"] = "anomaly_pinpoint"
    meta["anomaly_range"] = [i, j]
    meta["matched_anomaly"] = matched
    meta["replacement_word"] = repl
    meta["over_replaced"] = span_after != sb and (
        len(span_after) - len(sb) != len(repl) - len(matched)
    )
    return span_after, "anomaly_pinpoint", meta


def apply_answer_at_span(
    text: str, start: int, span_before: str, span_after: str | None
) -> str:
    end = start + len(span_before)
    if text[start:end] != span_before:
        raise ValueError(f"span mismatch at {start}: {span_before!r}")
    if span_after is None:
        return text[:start] + text[end:]
    return text[:start] + span_after + text[end:]


def apply_answers(
    text: str,
    answers: list[dict[str, Any]],
    *,
    chat_order: bool = False,
) -> tuple[str, list[dict[str, Any]]]:
    items = expand_answer_items_for_apply(list(answers))
    if chat_order:
        pn = [a for a in items if a.get("fact_class") == "proper_noun"]
        other = [a for a in items if a.get("fact_class") != "proper_noun"]
        items = pn + other

    work: list[dict[str, Any]] = []
    for item in items:
        ans = str(item.get("answer_text") or "").strip()
        if not ans:
            continue
        hint = int(item.get("span_start") or -1)
        start, matched_span = resolve_span(text, item)
        if start < 0:
            work.append({**item, "_apply_error": "span_not_found"})
            continue
        try:
            span_after, apply_mode, apply_meta = resolve_span_after(
                {**item, "span_before": matched_span},
                ans,
                hint_abs=hint,
                span_start=start,
            )
        except ValueError as e:
            work.append({**item, "_apply_error": str(e)})
            continue
        work.append(
            {
                **item,
                "span_before": matched_span,
                "span_start_resolved": start,
                "span_after": span_after,
                "apply_mode": apply_mode,
                "apply_meta": apply_meta,
                "verdict_applied": VERDICT_AUTO_DELETE if span_after is None else "auto_correct",
                "answer_text": ans,
            }
        )

    work.sort(
        key=lambda x: (
            -int(x.get("span_start_resolved") or -1),
            len(str(x.get("anomaly_word") or "")),
        )
    )
    out = text
    applied: list[dict[str, Any]] = []
    for item in work:
        if item.get("_apply_error"):
            applied.append(
                {
                    "question_id": item.get("question_id"),
                    "review_index": item.get("review_index"),
                    "anomaly_word": item.get("anomaly_word"),
                    "error": item["_apply_error"],
                }
            )
            continue
        ans = str(item.get("answer_text") or "").strip()
        hint = int(item.get("span_start") or -1)
        start, matched = resolve_span(out, item)
        if start < 0:
            applied.append(
                {
                    "question_id": item.get("question_id"),
                    "review_index": item.get("review_index"),
                    "anomaly_word": item.get("anomaly_word"),
                    "error": "span_not_found",
                }
            )
            continue
        try:
            span_after, apply_mode, apply_meta = resolve_span_after(
                {**item, "span_before": matched},
                ans,
                hint_abs=hint,
                span_start=start,
            )
        except ValueError as e:
            applied.append(
                {
                    "question_id": item.get("question_id"),
                    "review_index": item.get("review_index"),
                    "anomaly_word": item.get("anomaly_word"),
                    "error": str(e),
                }
            )
            continue
        if apply_mode == "anomaly_keep_unchanged":
            applied.append(
                {
                    "question_id": item.get("question_id"),
                    "review_index": item.get("review_index"),
                    "anomaly_word": item.get("anomaly_word"),
                    "answer_text": ans,
                    "verdict_applied": "keep",
                    "apply_mode": apply_mode,
                    "apply_meta": apply_meta,
                    "span_before": matched,
                    "span_after": matched,
                    "skipped": True,
                }
            )
            continue
        try:
            before = out[start : start + len(matched)]
            out = apply_answer_at_span(out, start, matched, span_after)
            applied.append(
                {
                    "question_id": item.get("question_id"),
                    "review_index": item.get("review_index"),
                    "anomaly_word": item.get("anomaly_word"),
                    "answer_text": ans,
                    "verdict_applied": VERDICT_AUTO_DELETE if span_after is None else "auto_correct",
                    "apply_mode": apply_mode,
                    "apply_meta": apply_meta,
                    "span_before": matched,
                    "span_after": span_after,
                    "before_excerpt": before[:80],
                }
            )
        except ValueError as e:
            applied.append(
                {
                    "question_id": item.get("question_id"),
                    "review_index": item.get("review_index"),
                    "anomaly_word": item.get("anomaly_word"),
                    "error": str(e),
                }
            )
    return out, applied
