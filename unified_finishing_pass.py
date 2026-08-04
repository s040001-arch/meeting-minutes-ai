"""Unified finishing pass: audit once, fix surgically, verify once.

従来の後段仕上げ（整文チャンク書き換え → 全文完成稿の書き換え → 最終レビュー
反復）は、検出と修正が全文書き換えの中に混在していた。全文書き換えは遅く、
新しい問題を持ち込み、段落単位のフォールバックで「直したはずの修正」が
巻き戻る原因だった。

このパスは検出と修正を分離する:

1. 監査   — 検出専用レビュアー（final_review と同じプロンプト）を、位置に
            よらない品質のため窓分割・並列で全文に走らせ、問題リストを得る。
2. トリアージ — 修正が一意な問題は自動修正へ、事実・人名・数値・判断不能な
            崩れは質問（LINE Q&A）へ。トリアージは minutes_quality_gate が
            残存 findings から質問を積む既存経路をそのまま使う。
3. 修正   — 全文書き換えはしない。apply_safe_fixes による1件ずつの
            ピンポイント置換（quote一意・長さ比・数値不変・事実ゲート）と、
            読者理解を妨げる崩れの外科的修復のみ。
4. 検証   — 修正後の全文に独立レビューを1回。残った問題は gate が
            ブロックし、質問として担当者へ送られる。

有効化: UNIFIED_FINISHING_ENABLED=1（既定は無効・従来パス）。
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import time
from typing import Any

UNIFIED_REPORT_FILENAME = "unified_finishing_report.json"
# 監査窓は検出品質が文書内の位置に依存しないための分割。窓ごとに全文脈は
# 見えないが、事実照合は検証段（全文1回）と gate が担う。
AUDIT_WINDOW_TARGET_CHARS = 12_000
AUDIT_MAX_PARALLEL = 3
# 読者阻害の崩れの外科的修復は1ラウンドで最大この件数まで扱う。
UNIFIED_RESOLVER_MAX_ITEMS = 60
# 長い無段落テキストの機械的な段落再割り（LLM不使用・事実安全）
REFLOW_PARAGRAPH_MAX_CHARS = 800
REFLOW_TARGET_CHARS = 400

_SENTENCE_END_RE = re.compile(r"(?<=[。！？!?])")
_PARAGRAPH_SEP_RE = re.compile(r"\n\s*\n+")


def is_unified_finishing_enabled() -> bool:
    raw = os.environ.get("UNIFIED_FINISHING_ENABLED", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _split_paragraphs(text: str) -> list[str]:
    return [part.strip() for part in _PARAGRAPH_SEP_RE.split(text) if part.strip()]


def reflow_long_paragraphs(text: str) -> str:
    """Mechanically split wall-of-text paragraphs at sentence boundaries.

    LLMを使わない決定的処理。文字は一切変更せず、段落区切りだけを足す。
    """
    out_paragraphs: list[str] = []
    for paragraph in _split_paragraphs(text):
        if len(paragraph) <= REFLOW_PARAGRAPH_MAX_CHARS or "\n" in paragraph:
            out_paragraphs.append(paragraph)
            continue
        sentences = [s for s in _SENTENCE_END_RE.split(paragraph) if s]
        group: list[str] = []
        group_len = 0
        for sentence in sentences:
            if group and group_len + len(sentence) > REFLOW_TARGET_CHARS:
                out_paragraphs.append("".join(group))
                group = []
                group_len = 0
            group.append(sentence)
            group_len += len(sentence)
        if group:
            out_paragraphs.append("".join(group))
    return "\n\n".join(out_paragraphs)


def _split_windows(text: str) -> list[str]:
    """Group paragraphs into audit windows of roughly equal size."""
    windows: list[str] = []
    current: list[str] = []
    current_len = 0
    for paragraph in _split_paragraphs(text):
        if current and current_len + len(paragraph) > AUDIT_WINDOW_TARGET_CHARS:
            windows.append("\n\n".join(current))
            current = []
            current_len = 0
        current.append(paragraph)
        current_len += len(paragraph)
    if current:
        windows.append("\n\n".join(current))
    return windows


def _audit_windows(
    text: str,
    meeting_profile: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[str], int]:
    """Run the detection-only reviewer over every window in parallel.

    返り値: (findings, errors, window数)。窓が1つでも失敗したままなら
    errors が残り、呼び出し側は fail-closed（gateブロック）にする。
    """
    from final_review_pass import _call_reviewer

    windows = _split_windows(text)
    errors: list[str] = []
    findings: list[dict[str, Any]] = []

    def audit_one(index: int, window: str) -> list[dict[str, Any]]:
        last_error: Exception | None = None
        for _attempt in range(2):
            try:
                return _call_reviewer(window, meeting_profile)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        raise RuntimeError(f"audit_window_{index}_failed:{last_error!r}")

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=AUDIT_MAX_PARALLEL
    ) as executor:
        futures = {
            executor.submit(audit_one, index, window): index
            for index, window in enumerate(windows)
        }
        results: dict[int, list[dict[str, Any]]] = {}
        for future in concurrent.futures.as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
    seen_quotes: set[str] = set()
    for index in sorted(results):
        for finding in results[index]:
            quote = str(finding.get("quote") or "").strip()
            if quote and quote in seen_quotes:
                continue
            if quote:
                seen_quotes.add(quote)
            findings.append(finding)
    return findings, errors, len(windows)


def run_unified_finishing(
    *,
    job_dir: str,
    text: str,
    meeting_profile: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Return (final_text, stats, final_review_compatible_report)."""
    from final_review_pass import (
        _call_reviewer,
        apply_safe_fixes,
        resolve_final_review_model,
        _write_report,
    )
    from editorial_transcript_pass import resolve_reader_blocking_findings

    started = time.monotonic()
    stats: dict[str, Any] = {
        "enabled": True,
        "attempted": False,
        "failed": False,
        "model": resolve_final_review_model(),
        "input_chars": len(text),
        "output_chars": len(text),
        "audit_windows": 0,
        "audit_findings": 0,
        "auto_applied": 0,
        "queued_candidates": 0,
        "verify_findings": 0,
        "duration_sec": 0.0,
    }
    report: dict[str, Any] = {
        "mode": "apply",
        "unified": True,
        "model": stats["model"],
        "input_chars": len(text),
        "findings": [],
        "applied": [],
        "skipped": [],
        "rounds": [],
    }
    if not text.strip():
        return text, stats, report
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        stats["failed"] = True
        report["error"] = "anthropic_api_key_missing"
        return text, stats, report

    stats["attempted"] = True
    profile = meeting_profile
    if profile is None:
        try:
            from meeting_profile import load_meeting_profile

            profile = load_meeting_profile(job_dir)
        except Exception as exc:  # noqa: BLE001
            print(f"unified_profile_load_failed={exc!r}")
            profile = {}

    out_text = reflow_long_paragraphs(text.strip()) + "\n"

    # 1. 監査: 検出専用・窓分割並列。修正はまだ行わない。
    try:
        audit_findings, audit_errors, window_count = _audit_windows(
            out_text, profile
        )
    except Exception as exc:  # noqa: BLE001
        stats["failed"] = True
        report["error"] = f"audit_failed:{exc!r}"
        _write_report(job_dir, report)
        return out_text, stats, report
    stats["audit_windows"] = window_count
    stats["audit_findings"] = len(audit_findings)
    if audit_errors:
        # 未監査の窓が残ったまま公開しない（fail-closed）。
        stats["failed"] = True
        report["error"] = f"audit_windows_failed:{'|'.join(audit_errors)}"
        _write_report(job_dir, report)
        return out_text, stats, report

    # 2+3. トリアージと修正: 1件ずつのピンポイント置換のみ。
    #      高確信は apply_safe_fixes、読者阻害の崩れは外科的修復
    #      （どちらも1件ごとに事実ゲート）。直せなかったものが findings に
    #      残り、gate が LINE 質問へ回す。
    def fix_round(
        text_in: str,
        findings: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
        text_out, applied, skipped = apply_safe_fixes(text_in, findings)
        applied_quotes = {str(f.get("quote") or "") for f in applied}
        unresolved = [
            f
            for f in findings
            if str(f.get("quote") or "") not in applied_quotes
        ]
        try:
            text_out, resolved, resolver_skipped = (
                resolve_reader_blocking_findings(
                    text=text_out,
                    findings=unresolved,
                    meeting_profile=profile,
                    force=True,
                    max_items=UNIFIED_RESOLVER_MAX_ITEMS,
                )
            )
            applied.extend(resolved)
            skipped.extend(resolver_skipped)
        except Exception as exc:  # noqa: BLE001
            print(f"unified_resolver_failed={exc!r}")
        return text_out, applied, skipped

    out_text, applied, skipped = fix_round(out_text, audit_findings)
    report["applied"] = applied
    report["skipped"] = skipped
    report["rounds"].append(
        {
            "round": 1,
            "stage": "audit_and_fix",
            "windows": window_count,
            "findings": audit_findings,
            "applied": applied,
            "skipped": skipped,
        }
    )

    # 4. 検証: 修正後の全文に独立レビュー。1回目の検証で見つかった問題は
    #    もう1度だけ外科的に修正し、その場合は最終検証で確認する
    #    （有界2ラウンド。全文書き換えの反復はしない）。
    remaining: list[dict[str, Any]] = []
    for verify_no in (2, 3):
        try:
            verify_findings = _call_reviewer(out_text, profile)
        except Exception as exc:  # noqa: BLE001
            stats["failed"] = True
            report["error"] = f"verify_failed:{exc!r}"
            _write_report(job_dir, report)
            return out_text, stats, report
        stats["verify_findings"] = len(verify_findings)
        if not verify_findings:
            remaining = []
            report["rounds"].append(
                {
                    "round": verify_no,
                    "stage": "verify",
                    "findings": [],
                    "applied": [],
                    "skipped": [],
                }
            )
            break
        if verify_no == 3:
            # 最終検証: 修正はせず、残りを質問へ。
            remaining = verify_findings
            report["rounds"].append(
                {
                    "round": verify_no,
                    "stage": "final_verify",
                    "findings": verify_findings,
                    "applied": [],
                    "skipped": [],
                }
            )
            break
        out_text, verify_applied, verify_skipped = fix_round(
            out_text, verify_findings
        )
        report["applied"].extend(verify_applied)
        report["rounds"].append(
            {
                "round": verify_no,
                "stage": "verify_and_fix",
                "findings": verify_findings,
                "applied": verify_applied,
                "skipped": verify_skipped,
            }
        )
        if not verify_applied:
            # 何も直せなかったなら再検証しても同じ結果になる。
            remaining = verify_findings
            break
    report["findings"] = remaining
    stats["auto_applied"] = len(report["applied"])
    stats["queued_candidates"] = len(remaining)
    stats["output_chars"] = len(out_text)
    stats["duration_sec"] = round(time.monotonic() - started, 1)

    _write_report(job_dir, report)
    try:
        path = os.path.join(job_dir, UNIFIED_REPORT_FILENAME)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {"stats": stats, "report": report},
                handle,
                ensure_ascii=False,
                indent=2,
            )
    except OSError as exc:
        print(f"unified_report_write_failed={exc!r}")
    print(
        "unified_finishing_done "
        f"windows={window_count} audit_findings={len(audit_findings)} "
        f"applied={len(report['applied'])} remaining={len(remaining)} "
        f"duration_sec={stats['duration_sec']}"
    )
    return out_text, stats, report
