"""Phase 4: incremental LINE answer reflection into merged_transcript_after_qa.txt."""
from __future__ import annotations

import json
import os
import shutil
import sys
from typing import Any, Callable

from recognition_batch import (
    VERIFY_TAG,
    _apply_delete_to_transcript,
    _is_standalone_word_at,
    find_standalone_word,
)
from span_correction import apply_span_word_replacement, resolve_word_position

AFTER_QA_FILENAME = "merged_transcript_after_qa.txt"
AI_TRANSCRIPT_FILENAME = "merged_transcript_ai.txt"
EXCERPT_RADIUS = 24


def after_qa_path(job_dir: str) -> str:
    return os.path.join(job_dir, AFTER_QA_FILENAME)


def ai_transcript_path(job_dir: str) -> str:
    return os.path.join(job_dir, AI_TRANSCRIPT_FILENAME)


def ensure_after_qa_initialized(job_dir: str, *, log: Callable[[str], None] | None = None) -> str:
    """Ensure after_qa exists; initialize from ai transcript when missing."""
    out = after_qa_path(job_dir)
    if os.path.isfile(out):
        return out
    ai = ai_transcript_path(job_dir)
    if not os.path.isfile(ai):
        raise FileNotFoundError(f"missing ai transcript for after_qa init: {ai}")
    os.makedirs(job_dir, exist_ok=True)
    shutil.copyfile(ai, out)
    msg = f"line_answer_reflect_init copied {AI_TRANSCRIPT_FILENAME} -> {AFTER_QA_FILENAME}"
    if log:
        log(msg)
    else:
        print(msg)
    return out


def load_after_qa_text(job_dir: str) -> str:
    path = ensure_after_qa_initialized(job_dir)
    with open(path, encoding="utf-8") as f:
        return f.read()


def save_after_qa_text(job_dir: str, text: str) -> None:
    path = ensure_after_qa_initialized(job_dir)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _excerpt(text: str, start: int, end: int, *, radius: int = EXCERPT_RADIUS) -> str:
    if start < 0:
        start = 0
    if end < start:
        end = start
    lo = max(0, start - radius)
    hi = min(len(text), end + radius)
    return text[lo:hi]


def _hint_pos(unknown_item: dict[str, Any]) -> int:
    for key in ("context_position_in_transcript", "span_start"):
        try:
            pos = int(unknown_item.get(key) or -1)
        except (TypeError, ValueError):
            continue
        if pos >= 0:
            return pos
    return -1


def _find_tagged_word_positions(text: str, word: str, *, hint_pos: int = -1) -> list[int]:
    """Standalone positions where ``word`` is immediately followed by VERIFY_TAG."""
    positions: list[int] = []
    start = 0
    tag_len = len(VERIFY_TAG)
    while start <= len(text):
        idx = text.find(word, start)
        if idx < 0:
            break
        if _is_standalone_word_at(text, idx, len(word)):
            if text[idx + len(word) : idx + len(word) + tag_len] == VERIFY_TAG:
                positions.append(idx)
        start = idx + 1 if idx >= start else start + 1
    if hint_pos >= 0 and len(positions) > 1:
        positions.sort(key=lambda i: abs(i - hint_pos))
    return positions


def _remove_tag_at(text: str, word_start: int, word: str) -> tuple[str, bool]:
    end = word_start + len(word)
    if text[end : end + len(VERIFY_TAG)] == VERIFY_TAG:
        return text[:end] + text[end + len(VERIFY_TAG) :], True
    return text, False


def _apply_keep_incremental(
    text: str,
    unknown_item: dict[str, Any],
    word: str,
) -> tuple[str, dict[str, Any] | None]:
    hint = _hint_pos(unknown_item)
    primary = resolve_word_position(
        text,
        word,
        hint_pos=hint,
        context=str(unknown_item.get("context") or ""),
    )
    if primary < 0:
        return text, None
    before = _excerpt(text, primary, primary + len(word) + len(VERIFY_TAG))
    updated, removed = _remove_tag_at(text, primary, word)
    if not removed:
        return text, None
    after = _excerpt(updated, primary, primary + len(word))
    return updated, {
        "action": "keep",
        "word": word,
        "span_start": primary,
        "span_end": primary + len(word),
        "before_excerpt": before,
        "after_excerpt": after,
        "tag_removed": True,
        "applied": True,
    }


def _apply_correct_incremental(
    text: str,
    unknown_item: dict[str, Any],
    word: str,
    correction: str,
) -> tuple[str, dict[str, Any] | None]:
    if not correction or correction == word:
        return _apply_keep_incremental(text, unknown_item, word)

    hint = _hint_pos(unknown_item)
    tagged = word + VERIFY_TAG
    positions = _find_tagged_word_positions(text, word, hint_pos=hint)
    if not positions:
        primary = resolve_word_position(
            text,
            word,
            hint_pos=hint,
            context=str(unknown_item.get("context") or ""),
        )
        if primary >= 0:
            positions = [primary]

    if not positions:
        return text, None

    out = text
    first_meta: dict[str, Any] | None = None
    for idx in sorted(positions, reverse=True):
        segment = tagged if out[idx : idx + len(tagged)] == tagged else word
        if out[idx : idx + len(segment)] != segment:
            continue
        before = _excerpt(out, idx, idx + len(segment))
        out, changed = apply_span_word_replacement(
            out, start=idx, wrong=segment, right=correction
        )
        if not changed:
            continue
        after = _excerpt(out, idx, idx + len(correction))
        meta = {
            "action": "correct",
            "word": word,
            "correction": correction,
            "span_start": idx,
            "span_end": idx + len(segment),
            "before_excerpt": before,
            "after_excerpt": after,
            "tag_removed": segment == tagged,
            "applied": True,
        }
        if first_meta is None:
            first_meta = meta

    return out, first_meta


def apply_incremental_coherence_answer(
    text: str,
    *,
    unknown_item: dict[str, Any],
    parsed: dict[str, Any],
    question_id: str = "",
) -> tuple[str, dict[str, Any]]:
    """Apply one LINE coherence answer to current after_qa text."""
    word = str(parsed.get("word") or unknown_item.get("anomaly_word") or "").strip()
    action = str(parsed.get("action") or "unknown").strip().lower()
    correction = str(parsed.get("correction") or "").strip()
    anomaly_id = str(unknown_item.get("anomaly_id") or "").strip()

    base_meta: dict[str, Any] = {
        "question_id": question_id,
        "anomaly_id": anomaly_id,
        "word": word,
        "action": action,
        "correction": correction,
        "applied": False,
        "tag_removed": False,
    }

    if not word:
        base_meta["reason"] = "missing_word"
        return text, base_meta

    if action == "unknown":
        base_meta["reason"] = "unknown_action"
        return text, base_meta

    if action == "delete":
        before_text = text
        updated, deleted = _apply_delete_to_transcript(
            text,
            span_hint=correction,
            word=word,
        )
        if not deleted:
            base_meta["reason"] = "delete_span_not_found"
            return text, base_meta
        base_meta.update(
            {
                "applied": True,
                "action": "delete",
                "before_excerpt": str(deleted.get("before") or "")[:120],
                "after_excerpt": "",
                "tag_removed": VERIFY_TAG in str(deleted.get("before") or ""),
                "span_start": -1,
                "span_end": -1,
            }
        )
        if updated != before_text:
            return updated, base_meta
        base_meta["applied"] = False
        base_meta["reason"] = "delete_no_change"
        return text, base_meta

    if action == "keep":
        updated, meta = _apply_keep_incremental(text, unknown_item, word)
        if meta:
            meta["question_id"] = question_id
            meta["anomaly_id"] = anomaly_id
            return updated, meta
        base_meta["reason"] = "keep_tag_not_found"
        return text, base_meta

    if action == "correct":
        updated, meta = _apply_correct_incremental(text, unknown_item, word, correction)
        if meta:
            meta["question_id"] = question_id
            meta["anomaly_id"] = anomaly_id
            return updated, meta
        base_meta["reason"] = "correct_span_not_found"
        return text, base_meta

    base_meta["reason"] = f"unsupported_action={action}"
    return text, base_meta


def format_reflect_log(entry: dict[str, Any]) -> str:
    return f"line_answer_reflect_applied={json.dumps(entry, ensure_ascii=False)}"


def log_reflect_entry(entry: dict[str, Any], *, log: Callable[[str], None] | None = None) -> None:
    line = format_reflect_log(entry)
    if log:
        log(line)
        return
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((line + "\n").encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()
