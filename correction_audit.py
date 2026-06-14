"""Correction/delete audit log and conservative coherence auto-delete (Phase 2)."""
from __future__ import annotations

import json
import os
from typing import Any

from recognition_batch import find_standalone_word

CORRECTION_AUDIT_LOG_FILENAME = "correction_audit_log.json"
AUTO_DELETE_INLINE_MARKER = "[削除]"
COHERENCE_AUTO_DELETE_MODE_ENV = "COHERENCE_AUTO_DELETE_MODE"
DEFAULT_AUTO_DELETE_MODE = "shadow"
MAX_AUTO_DELETE_SPAN_CHARS = 24

_CLEAR_NON_CONTENT_NATURES = frozenset(
    {"filler", "backchannel", "hesitation", "metaphor", "colloquial", "interjection"}
)

AUDIT_SECTION_HEADING = "## 補正・削除監査ログ"


def resolve_coherence_auto_delete_mode() -> str:
    mode = os.environ.get(COHERENCE_AUTO_DELETE_MODE_ENV, DEFAULT_AUTO_DELETE_MODE).strip().lower()
    return mode if mode in {"shadow", "active"} else DEFAULT_AUTO_DELETE_MODE


def normalize_materiality(value: object) -> str:
    m = str(value or "high").strip().lower()
    return m if m in {"high", "low"} else "high"


def normalize_content_nature(value: object) -> str:
    n = str(value or "unknown").strip().lower()
    allowed = _CLEAR_NON_CONTENT_NATURES | {"substantive", "unknown"}
    return n if n in allowed else "unknown"


def is_auto_delete_candidate(anomaly: dict[str, Any]) -> bool:
    """Conservative gate: low materiality + clearly non-content only."""
    if normalize_materiality(anomaly.get("materiality")) != "low":
        return False
    nature = normalize_content_nature(anomaly.get("content_nature"))
    if nature not in _CLEAR_NON_CONTENT_NATURES:
        return False
    if str(anomaly.get("confidence") or "").lower() != "low":
        return False
    return True


def classify_anomaly_routing(anomaly: dict[str, Any]) -> str:
    """Return question | tag_only | auto_fix | auto_delete_candidate."""
    if anomaly.get("auto_fixable"):
        return "auto_fix"
    if is_auto_delete_candidate(anomaly):
        return "auto_delete_candidate"
    conf = str(anomaly.get("confidence") or "low").lower()
    if conf in {"medium", "high"}:
        return "question"
    return "tag_only"


def load_audit_log(job_dir: str) -> list[dict[str, Any]]:
    path = os.path.join(job_dir, CORRECTION_AUDIT_LOG_FILENAME)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
    except (OSError, json.JSONDecodeError):
        pass
    return []


def write_audit_log(path: str, entries: list[dict[str, Any]]) -> None:
    rows = [e for e in entries if _audit_entry_valid(e)]
    if not rows:
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


def append_audit_entries(job_dir: str, new_entries: list[dict[str, Any]]) -> int:
    path = os.path.join(job_dir, CORRECTION_AUDIT_LOG_FILENAME)
    existing = load_audit_log(job_dir)
    existing_ids = {str(e.get("anomaly_id") or "") for e in existing if e.get("anomaly_id")}
    added = 0
    for entry in new_entries:
        if not _audit_entry_valid(entry):
            continue
        aid = str(entry.get("anomaly_id") or "")
        if aid and aid in existing_ids:
            continue
        existing.append(entry)
        if aid:
            existing_ids.add(aid)
        added += 1
    if added:
        write_audit_log(path, existing)
    return added


def merge_audit_entries(path: str, new_entries: list[dict[str, Any]]) -> None:
    """Merge new entries into an audit log file at ``path``."""
    existing: list[dict[str, Any]] = []
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                existing = [x for x in data if isinstance(x, dict)]
        except (OSError, json.JSONDecodeError):
            existing = []
    existing_ids = {str(e.get("anomaly_id") or "") for e in existing if e.get("anomaly_id")}
    for entry in new_entries:
        if not _audit_entry_valid(entry):
            continue
        aid = str(entry.get("anomaly_id") or "")
        if aid and aid in existing_ids:
            continue
        existing.append(entry)
        if aid:
            existing_ids.add(aid)
    write_audit_log(path, existing)


def _audit_entry_valid(entry: dict[str, Any]) -> bool:
    action = str(entry.get("action") or "").strip().lower()
    return action in {"correct", "delete"}


def _resolve_delete_span(text: str, anomaly: dict[str, Any]) -> tuple[int, int, str]:
    """Return minimal verbatim span for conservative auto-delete (no clause expansion)."""
    word = str(anomaly.get("anomaly_word") or "").strip()
    hint = int(anomaly.get("span_start") or anomaly.get("context_position_in_transcript") or -1)
    if word:
        idx = find_standalone_word(text, word, hint_pos=hint)
        if idx >= 0:
            snippet = text[idx : idx + len(word)]
            if len(snippet) <= MAX_AUTO_DELETE_SPAN_CHARS:
                return idx, idx + len(word), snippet
    try:
        start = int(anomaly.get("span_start") or -1)
        end = int(anomaly.get("span_end") or -1)
    except (TypeError, ValueError):
        start, end = -1, -1
    if start >= 0 and end > start and end <= len(text):
        snippet = text[start:end]
        if snippet.strip() and len(snippet) <= MAX_AUTO_DELETE_SPAN_CHARS:
            if not word or word in snippet or snippet in word:
                return start, end, snippet
    span_text = str(anomaly.get("span_text") or "").strip().strip("…")
    if span_text and len(span_text) <= MAX_AUTO_DELETE_SPAN_CHARS and span_text in text:
        idx = text.find(span_text)
        return idx, idx + len(span_text), span_text
    return -1, -1, ""


def build_auto_delete_audit_entry(
    text: str,
    anomaly: dict[str, Any],
    *,
    mode: str,
    applied: bool,
) -> dict[str, Any] | None:
    if not is_auto_delete_candidate(anomaly):
        return None
    start, end, original = _resolve_delete_span(text, anomaly)
    if start < 0 or not original.strip():
        return None
    if len(original) > MAX_AUTO_DELETE_SPAN_CHARS:
        return None
    return {
        "anomaly_id": anomaly.get("anomaly_id"),
        "action": "delete",
        "before": original,
        "after": AUTO_DELETE_INLINE_MARKER if applied else "",
        "confidence": anomaly.get("confidence"),
        "materiality": normalize_materiality(anomaly.get("materiality")),
        "content_nature": normalize_content_nature(anomaly.get("content_nature")),
        "reason": str(anomaly.get("reason") or "低材料性の非内容語").strip(),
        "span_start": start,
        "span_end": end,
        "span_text": original,
        "span_corrected": "",
        "source": "coherence_auto_delete",
        "delete_mode": mode,
        "applied": applied,
        "inline_marker": applied,
        "restore_text": original,
    }


def apply_conservative_auto_deletes(
    text: str,
    anomalies: list[dict[str, Any]],
    *,
    mode: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Apply or shadow-log conservative auto-deletes. Returns (text, audit entries)."""
    resolved_mode = mode or resolve_coherence_auto_delete_mode()
    active = resolved_mode == "active"
    candidates: list[tuple[int, dict[str, Any]]] = []
    for an in anomalies:
        if not is_auto_delete_candidate(an):
            continue
        start, end, original = _resolve_delete_span(text, an)
        if start < 0 or not original.strip():
            continue
        candidates.append((start, an))
    candidates.sort(key=lambda x: x[0], reverse=True)

    out = text
    entries: list[dict[str, Any]] = []
    for start, an in candidates:
        entry = build_auto_delete_audit_entry(out, an, mode=resolved_mode, applied=active)
        if not entry:
            continue
        if active:
            s = int(entry["span_start"])
            e = int(entry["span_end"])
            out = out[:s] + AUTO_DELETE_INLINE_MARKER + out[e:]
        entries.append(entry)
    entries.reverse()
    return out, entries


def format_audit_entry_md(entry: dict[str, Any]) -> str:
    action = str(entry.get("action") or "").strip().lower()
    aid = str(entry.get("anomaly_id") or "—")
    label = "修正" if action == "correct" else "削除"
    source = str(entry.get("source") or "").strip()
    if action == "delete" and source == "coherence_auto_delete":
        if entry.get("applied"):
            label = "削除（AI自動・適用済）"
        else:
            label = "削除（AI自動・shadow候補）"
    elif action == "delete" and source == "manual_line_answer":
        label = "削除（手動・LINE回答）"

    before = str(entry.get("before") or entry.get("restore_text") or "").strip()
    after = str(entry.get("after") or "").strip()
    reason = str(entry.get("reason") or "").strip()
    try:
        pos = f"{entry.get('span_start')}–{entry.get('span_end')}"
    except (TypeError, ValueError):
        pos = "—"

    lines = [f"### {aid} — {label}", f"- **変更前:** {before or '（なし）'}"]
    if action == "correct":
        lines.append(f"- **変更後:** {after or '（なし）'}")
    elif action == "delete":
        lines.append(f"- **削除後:** {after or '（除去）'}")
        lines.append(f"- **復元用原文:** {before or '（なし）'}")
    if reason:
        lines.append(f"- **理由:** {reason}")
    lines.append(f"- **位置:** {pos}")
    mat = str(entry.get("materiality") or "").strip()
    if mat:
        lines.append(f"- **材料性:** {mat}")
    return "\n".join(lines)


def format_audit_section_md(entries: list[dict[str, Any]]) -> str:
    valid = [e for e in entries if _audit_entry_valid(e)]
    if not valid:
        return ""
    blocks = [format_audit_entry_md(e) for e in valid]
    intro = (
        f"{AUDIT_SECTION_HEADING}\n\n"
        "逐語への変更履歴（keep は含まない）。"
        "削除エントリは復元用原文・位置を保持します。\n"
    )
    return intro + "\n\n".join(blocks) + "\n"


def append_audit_section_to_structured_md(content: str, job_dir: str) -> str:
    """Append audit log section to minutes_structured.md (not Hub Doc builder)."""
    entries = load_audit_log(job_dir)
    section = format_audit_section_md(entries)
    if not section:
        return content
    base = content.rstrip()
    if AUDIT_SECTION_HEADING in base:
        return base
    return base + "\n\n" + section


def build_manual_delete_audit_entry(
    *,
    anomaly_id: str,
    before: str,
    word: str,
    reason: str = "ユーザー回答: 削除",
) -> dict[str, Any]:
    return {
        "anomaly_id": anomaly_id,
        "action": "delete",
        "before": before,
        "after": "",
        "reason": reason,
        "source": "manual_line_answer",
        "applied": True,
        "inline_marker": False,
        "restore_text": before,
        "word": word,
    }
