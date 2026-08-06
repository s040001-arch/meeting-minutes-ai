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
import re
from pathlib import Path
from typing import Any

from final_review_pass import FINAL_REVIEW_MAX_FINDINGS

QUALITY_GATE_REPORT_FILENAME = "minutes_quality_gate.json"
_VALID_MODES = {"off", "report", "enforce"}


class MinutesQualityGateError(RuntimeError):
    """Raised when an enforce-mode transcript is not publishable."""


# 読者の理解を実際に阻害する検出だけを質問・ブロック対象にするための判定。
# type ベース + issue 文言ベースの二段。
_READER_BLOCKING_TYPES = {"fragment", "contradiction"}
_READER_BLOCKING_ISSUE_RE = re.compile(
    r"意味不明|意味が取れない|意味を取れない|理解できない|理解不能"
    r"|成立していない|成立しない|破綻|矛盾|意味をなさない|判読できない"
    # 人名の誤りは読者は気づけず事実性に直結するため、質問対象に含める
    r"|人名|氏名"
)


def _needs_user_attention(finding: dict[str, Any]) -> bool:
    """True if an unresolved finding must block publication and be asked.

    ユーザー方針の変遷:
    - 2026-08-05: 未解決は確信度によらずすべて質問へ（放置しない）。
    - 2026-08-06 改訂: 上記の運用で「読めば理解できる文の文体的違和感」まで
      質問が殺到した（楽天ジョブで45問超の質問洪水）。質問・ブロックの対象は
      「読者の理解を実際に阻害する崩れ」（断片・意味不明・矛盾）に限定する。
      理解可能だが不自然なだけの残存は warning として記録し、公開は妨げない。
    """
    quote = str(finding.get("quote") or "").strip()
    if not quote:
        return False
    ftype = str(finding.get("type") or "").strip().lower()
    if ftype in _READER_BLOCKING_TYPES:
        return True
    issue = str(finding.get("issue") or "")
    return bool(_READER_BLOCKING_ISSUE_RE.search(issue))


def _quotes_overlap(a: str, b: str) -> bool:
    """2つの引用が同じ箇所を指しているか（境界ゆれを同一視する）。

    2026-08-06: LLM の再監査は同じ箇所を毎回微妙に違う引用範囲で返すため、
    完全一致の重複排除では同じ質問が別項目として増殖した（楽天ジョブで
    75→183件）。包含または長い共通部分文字列があれば同一箇所とみなす。
    """
    a = (a or "").strip()
    b = (b or "").strip()
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    from difflib import SequenceMatcher

    match = SequenceMatcher(None, a, b).find_longest_match(0, len(a), 0, len(b))
    return match.size >= 12


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
    # 重なり判定用: final_review 由来の既存項目（回答済み含む）の引用一覧。
    # 回答済みの箇所を再監査が別範囲で再検出しても、二度と聞き直さない。
    final_existing = [
        x for x in existing if str(x.get("source") or "") == "final_review"
    ]
    queued = 0
    for finding in findings:
        quote = str(finding.get("quote") or "").strip()
        if not quote or quote not in text:
            continue
        issue = str(finding.get("issue") or "").strip()
        digest = hashlib.sha256(f"{quote}\0{issue}".encode("utf-8")).hexdigest()[:16]
        anomaly_id = f"final_{digest}"
        item = by_id.get(anomaly_id)
        if item is None:
            overlap = next(
                (
                    x
                    for x in final_existing
                    if _quotes_overlap(
                        quote, str(x.get("text") or x.get("anomaly_word") or "")
                    )
                ),
                None,
            )
            if overlap is not None:
                status = str(overlap.get("status") or "").strip().lower()
                if status in terminal_statuses:
                    # 同じ箇所は回答済み。再質問しない。
                    continue
                # 未回答の同一箇所: 既存項目を活かす（新規追加しない）。
                item = overlap
                anomaly_id = str(overlap.get("anomaly_id") or anomaly_id)
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
            # 同一実行内の後続 findings とも重なり判定できるようにする
            final_existing.append(payload)
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
    ai_correction_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stats = readable_stats or {}
    final_report = stats.get("final_review")
    if not isinstance(final_report, dict):
        final_report = {}

    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    # AI補正（Step 4.3 全文書き直し）が丸ごとフォールバックした場合は
    # ブロックする（2026-08-05）。楽天ジョブで Anthropic 過負荷により
    # AI補正なしの原文が最後まで素通りし、大量の意味不明語が残った。
    # 後段の検出はAI補正済みテキストを前提にしており、代替にならない。
    if isinstance(ai_correction_meta, dict) and ai_correction_meta.get(
        "used_fallback"
    ):
        blockers.append(
            {
                "code": "ai_correction_fallback",
                "message": (
                    "AI補正（全文書き直し）が失敗し原文のまま通過した。"
                    "再処理が必要"
                ),
                "fallback_reason": str(
                    ai_correction_meta.get("fallback_reason") or ""
                ),
            }
        )

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
    # 理解可能だが不自然なだけの残存は警告に留める（2026-08-06）。
    unresolved_minor = [
        f
        for f in findings
        if str(f.get("quote") or "").strip() and not _needs_user_attention(f)
    ]
    if unresolved_minor:
        warnings.append(
            {
                "code": "minor_wording_unresolved",
                "message": (
                    "文体・表記レベルの違和感が残っているが、"
                    "読者の理解は阻害しないため公開は妨げない"
                ),
                "count": len(unresolved_minor),
                "examples": unresolved_minor[:10],
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
    # 確定修正ペアは LINE 監査だけでなく全ソース（バッチ回答・トリアージ・
    # ナレッジ自己解決・自動修正）から集約する（2026-08-05）。
    # 楽天ジョブで「山谷」等のバッチ回答由来ペアの残存を見逃した再発防止。
    audit_rows: list[dict[str, Any]] = []
    try:
        from confirmed_corrections import collect_confirmed_pairs

        audit_rows = [
            {"wrong": p["wrong"], "correct": p["right"], "source": p["source"]}
            for p in collect_confirmed_pairs(job_dir)
        ]
    except Exception as exc:  # noqa: BLE001
        print(f"quality_gate_confirmed_pairs_failed={exc!r}")
        audit_path = Path(job_dir) / "line_correction_audit.jsonl"
        if audit_path.is_file():
            for line in audit_path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    audit_rows.append(row)
    ai_correction_meta: dict[str, Any] | None = None
    meta_path = Path(job_dir) / "correction_meta.json"
    if meta_path.is_file():
        try:
            loaded_meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(loaded_meta, dict):
                ai_correction_meta = loaded_meta
        except (OSError, json.JSONDecodeError):
            ai_correction_meta = None
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
            ai_correction_meta=ai_correction_meta,
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
