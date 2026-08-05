"""Fail-closed quality gate for the finished readable transcript.

The pipeline used to publish even when readable chunks fell back to raw text or
the final reviewer reported known defects.  This gate turns those conditions
into a structured report and, in ``enforce`` mode, stops before Google Docs is
updated.
"""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
from typing import Any

from final_review_pass import FINAL_REVIEW_MAX_FINDINGS

QUALITY_GATE_REPORT_FILENAME = "minutes_quality_gate.json"
_VALID_MODES = {"off", "report", "enforce"}


class MinutesQualityGateError(RuntimeError):
    """Raised when an enforce-mode transcript is not publishable."""


def _needs_user_attention(finding: dict[str, Any]) -> bool:
    """True if an unresolved finding must block publication and be asked.

    ユーザー方針（2026-08-05確定）: 検出した違和感は「読みやすく修正する」か
    「質問する」の二択で、確信度が低いからといって記録だけで放置しない。
    修正段（外科的修復・段落修復）を通っても直せなかった残存問題は、
    確信度によらずすべて質問へ回す。質問できる surface（quote）が無い
    findings だけは質問化できないため対象外。
    """
    quote = str(finding.get("quote") or "").strip()
    return bool(quote)


def _queue_unresolved_final_findings(
    *,
    job_dir: str,
    text: str,
    readable_stats: dict[str, Any] | None,
) -> int:
    """Expose unresolved final-review findings to the normal Q&A cycle."""
    stats = readable_stats or {}
    final_report = stats.get("final_review")
    if not isinstance(final_report, dict):
        return 0
    findings = [
        f
        for f in (final_report.get("findings") or [])
        if isinstance(f, dict) and _needs_user_attention(f)
    ]
    if not findings:
        return 0

    path = Path(job_dir) / "unknown_points.json"
    existing: list[dict[str, Any]] = []
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                existing = [x for x in loaded if isinstance(x, dict)]
        except (OSError, json.JSONDecodeError):
            existing = []
    terminal_statuses = {"answered", "done", "closed", "resolved"}
    for item in existing:
        if str(item.get("status") or "").strip().lower() in terminal_statuses:
            continue
        source = str(item.get("source") or "").strip()
        surface_keys = (
            ("anomaly_word", "text", "span_text")
            if source == "final_review"
            else ("anomaly_word", "span_text")
        )
        surfaces = [str(item.get(key) or "").strip() for key in surface_keys]
        surfaces = [surface for surface in surfaces if surface]
        if surfaces and not any(surface in text for surface in surfaces):
            item["status"] = "resolved"
            item["resolved_via"] = "final_readable_text"
    by_id = {
        str(x.get("anomaly_id") or ""): x
        for x in existing
        if str(x.get("anomaly_id") or "")
    }
    queued = 0
    for finding in findings:
        quote = str(finding.get("quote") or "").strip()
        if not quote or quote not in text:
            continue
        issue = str(finding.get("issue") or "").strip()
        digest = hashlib.sha256(f"{quote}\0{issue}".encode("utf-8")).hexdigest()[:16]
        anomaly_id = f"final_{digest}"
        item = by_id.get(anomaly_id)
        payload = {
            "type": "final_review",
            "source": "final_review",
            "anomaly_id": anomaly_id,
            "anomaly_word": quote,
            "text": quote,
            "span_text": quote,
            "issue": issue,
            "estimated_correction": str(finding.get("fix") or "").strip(),
            "confidence": str(finding.get("confidence") or "").lower(),
            "context_position_in_transcript": text.find(quote),
            "status": "open",
        }
        if item is None:
            existing.append(payload)
            by_id[anomaly_id] = payload
        else:
            item.update(payload)
        queued += 1
    if queued:
        path.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return queued


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

    editorial_stats = stats.get("editorial_transcript")
    if isinstance(editorial_stats, dict) and editorial_stats.get("enabled"):
        if editorial_stats.get("failed"):
            blockers.append(
                {
                    "code": "editorial_transcript_failed",
                    "message": "読者向け全文完成稿の生成または事実保持検証に失敗した",
                    "errors": list(
                        editorial_stats.get("validation_errors") or []
                    )[:20],
                }
            )
        editorial_fallback = list(
            editorial_stats.get("fallback_chunk_idx") or []
        )
        if editorial_fallback:
            warnings.append(
                {
                    "code": "editorial_partial_fallback",
                    "message": "全文完成稿の一部は入力整文を維持し、最終レビューへ回した",
                    "chunk_indices": editorial_fallback,
                }
            )
    else:
        editorial_fallback = []

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
    unresolved_blocking = [f for f in findings if _needs_user_attention(f)]
    if unresolved_blocking:
        blockers.append(
            {
                "code": "final_review_unresolved",
                "message": "最終レビューの理解阻害・要確認問題が未解決",
                "count": len(unresolved_blocking),
                "examples": unresolved_blocking[:10],
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
        1
        for f in findings
        if str(f.get("confidence") or "").lower() == "low"
        and not _needs_user_attention(f)
    )
    if low_count:
        warnings.append(
            {
                "code": "final_review_low",
                "message": "low確信の軽微な違和感が残っている（公開は妨げない）",
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
    # 公開をブロックすべきは「本文の崩れ・表記」に関する未回答のみ。
    # 検出段の「主語が曖昧」「数値が不明確」等は発言そのものの曖昧さで、
    # 質問選択器が価値判断でスキップしうる。これをブロッカーに数えると
    # 「質問しないのにゲートが塞ぐ」デッドロックになる（2026-08-05 楽天で
    # 実際に発生）ため、警告に格下げする。
    comprehension_sources = {
        "coherence_review",
        "final_review",
        "recognition_batch",
    }
    active_pending: list[dict[str, Any]] = []
    vague_pending: list[dict[str, Any]] = []
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
        if not any(surface in text for surface in surfaces):
            continue
        src = str(item.get("source") or "").strip()
        typ = str(item.get("type") or "").strip()
        if src in comprehension_sources or typ in comprehension_sources:
            active_pending.append(item)
        else:
            vague_pending.append(item)
    if active_pending:
        blockers.append(
            {
                "code": "pending_unknowns_remaining",
                "message": "未回答の確認事項に対応する本文が残っている",
                "count": len(active_pending),
                "examples": active_pending[:10],
            }
        )
    if vague_pending:
        warnings.append(
            {
                "code": "content_vagueness_unasked",
                "message": (
                    "発言自体の曖昧さ（主語・数値など）が残っているが、"
                    "質問価値が低いため公開は妨げない"
                ),
                "count": len(vague_pending),
                "examples": vague_pending[:10],
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
            "editorial_enabled": bool(
                isinstance(editorial_stats, dict)
                and editorial_stats.get("enabled")
            ),
            "editorial_applied": bool(
                isinstance(editorial_stats, dict)
                and editorial_stats.get("applied")
            ),
            "editorial_failed": bool(
                isinstance(editorial_stats, dict)
                and editorial_stats.get("failed")
            ),
            "editorial_fallback_chunks": len(editorial_fallback),
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
    if report["status"] == "blocked":
        queued = _queue_unresolved_final_findings(
            job_dir=job_dir,
            text=text,
            readable_stats=readable_stats,
        )
        report["metrics"]["final_review_questions_queued"] = queued
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
