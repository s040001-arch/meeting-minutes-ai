"""Fail-closed quality gate for the finished readable transcript.

The pipeline used to publish even when readable chunks fell back to raw text or
the final reviewer reported known defects.  This gate turns those conditions
into a structured report and, in ``enforce`` mode, stops before Google Docs is
updated.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from final_review_pass import FINAL_REVIEW_MAX_FINDINGS

QUALITY_GATE_REPORT_FILENAME = "minutes_quality_gate.json"
_VALID_MODES = {"off", "report", "enforce"}


class MinutesQualityGateError(RuntimeError):
    """Raised when an enforce-mode transcript is not publishable."""


def resolve_minutes_quality_gate_mode() -> str:
    raw = os.environ.get("MINUTES_QUALITY_GATE_MODE", "").strip().lower()
    return raw if raw in _VALID_MODES else "report"


def evaluate_minutes_quality(
    *,
    text: str,
    readable_stats: dict[str, Any] | None,
    correction_audit_rows: list[dict[str, Any]] | None = None,
    unknown_points: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    stats = readable_stats or {}
    final_report = stats.get("final_review")
    if not isinstance(final_report, dict):
        final_report = {}

    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    failed_chunks = list(stats.get("failed_chunk_idx") or [])
    if failed_chunks:
        blockers.append(
            {
                "code": "readable_chunk_fallback",
                "message": "整文失敗チャンクが原文へフォールバックした",
                "chunk_indices": failed_chunks,
            }
        )

    if final_report.get("error"):
        blockers.append(
            {
                "code": "final_review_error",
                "message": "最終レビューが完了していない",
                "error": str(final_report.get("error")),
            }
        )

    review_mode = str(final_report.get("mode") or "")
    if review_mode and review_mode != "apply":
        blockers.append(
            {
                "code": "final_review_not_apply",
                "message": f"最終レビューが apply ではない: {review_mode}",
            }
        )

    findings = [
        f for f in (final_report.get("findings") or []) if isinstance(f, dict)
    ]
    unresolved_high_medium = [
        f
        for f in findings
        if str(f.get("confidence") or "").lower() in {"high", "medium"}
    ]
    if unresolved_high_medium:
        blockers.append(
            {
                "code": "final_review_unresolved",
                "message": "最終レビューのhigh/medium問題が未解決",
                "count": len(unresolved_high_medium),
                "examples": unresolved_high_medium[:10],
            }
        )

    if len(findings) >= FINAL_REVIEW_MAX_FINDINGS:
        blockers.append(
            {
                "code": "final_review_saturated",
                "message": "検出件数が上限に達しており、後続問題が未走査の可能性がある",
                "count": len(findings),
            }
        )

    low_count = sum(
        1 for f in findings if str(f.get("confidence") or "").lower() == "low"
    )
    if low_count:
        warnings.append(
            {
                "code": "final_review_low",
                "message": "low確信の違和感が残っている",
                "count": low_count,
            }
        )

    verify_tag_count = text.count("[要確認]")
    if verify_tag_count:
        blockers.append(
            {
                "code": "verify_tags_remaining",
                "message": "回答完了後の本文に[要確認]が残っている",
                "count": verify_tag_count,
            }
        )

    terminal_statuses = {"answered", "done", "closed", "resolved"}
    active_pending: list[dict[str, Any]] = []
    for item in unknown_points or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "").strip().lower() in terminal_statuses:
            continue
        surfaces = [
            str(item.get(key) or "").strip()
            for key in ("anomaly_word", "text", "span_text")
        ]
        surfaces = [s for s in surfaces if s]
        if any(surface in text for surface in surfaces):
            active_pending.append(item)
    if active_pending:
        blockers.append(
            {
                "code": "pending_unknowns_remaining",
                "message": "未回答の確認事項に対応する本文が残っている",
                "count": len(active_pending),
                "examples": active_pending[:10],
            }
        )

    latest_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for row in correction_audit_rows or []:
        if not isinstance(row, dict):
            continue
        wrong = str(row.get("wrong") or "")
        correct = str(row.get("correct") or "")
        if wrong and correct:
            latest_pair[(wrong, correct)] = row
    unresolved_pairs = [
        {
            "wrong": wrong,
            "correct": correct,
            "remaining": text.count(wrong),
        }
        for wrong, correct in latest_pair
        if wrong in text
    ]
    if unresolved_pairs:
        blockers.append(
            {
                "code": "confirmed_corrections_unapplied",
                "message": "LINEで確定した修正前表記が本文に残っている",
                "items": unresolved_pairs[:20],
            }
        )

    return {
        "status": "blocked" if blockers else "pass",
        "blockers": blockers,
        "warnings": warnings,
        "metrics": {
            "text_chars": len(text),
            "readable_total_chunks": int(stats.get("total_chunks") or 0),
            "readable_failed_chunks": len(failed_chunks),
            "readable_split_recovered": int(stats.get("split_recovered") or 0),
            "final_review_findings": len(findings),
            "final_review_applied": len(final_report.get("applied") or []),
            "verify_tag_count": verify_tag_count,
            "active_pending_unknowns": len(active_pending),
        },
    }


def run_minutes_quality_gate(
    *,
    job_dir: str,
    text: str,
    readable_stats: dict[str, Any] | None,
) -> dict[str, Any]:
    mode = resolve_minutes_quality_gate_mode()
    audit_rows: list[dict[str, Any]] = []
    audit_path = Path(job_dir) / "line_correction_audit.jsonl"
    if audit_path.is_file():
        for line in audit_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                audit_rows.append(row)
    unknown_points: list[dict[str, Any]] = []
    unknowns_path = Path(job_dir) / "unknown_points.json"
    if unknowns_path.is_file():
        try:
            loaded = json.loads(unknowns_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                unknown_points = [x for x in loaded if isinstance(x, dict)]
        except (OSError, json.JSONDecodeError):
            unknown_points = []
    report = {
        "mode": mode,
        **evaluate_minutes_quality(
            text=text,
            readable_stats=readable_stats,
            correction_audit_rows=audit_rows,
            unknown_points=unknown_points,
        ),
    }
    path = Path(job_dir) / QUALITY_GATE_REPORT_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        "minutes_quality_gate="
        f"{report['status']} blockers={len(report['blockers'])} "
        f"warnings={len(report['warnings'])} mode={mode}"
    )
    if mode == "enforce" and report["status"] != "pass":
        codes = ",".join(str(x.get("code") or "") for x in report["blockers"])
        raise MinutesQualityGateError(f"minutes quality gate blocked: {codes}")
    return report
