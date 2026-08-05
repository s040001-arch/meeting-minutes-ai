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

import anthropic

UNIFIED_REPORT_FILENAME = "unified_finishing_report.json"
# 監査窓は検出品質が文書内の位置に依存しないための分割。窓ごとに全文脈は
# 見えないが、事実照合は検証段（全文1回）と gate が担う。
AUDIT_WINDOW_TARGET_CHARS = 12_000
AUDIT_MAX_PARALLEL = 3
# 読者阻害の崩れの外科的修復は1ラウンドで最大この件数まで扱う。
UNIFIED_RESOLVER_MAX_ITEMS = 60
# 未解決の読者阻害がこの件数以上集中する段落は、段落単位で修復する。
# スパン置換では直せない「段落まるごと崩れ」への限定的なフォールバック。
DENSE_PARAGRAPH_MIN_FINDINGS = 2
DENSE_REPAIR_MAX_PARALLEL = 3
DENSE_REPAIR_TIMEOUT_SEC = 600
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


ANSWERED_KNOWLEDGE_MAX_ITEMS = 40


def _answered_knowledge_block(job_dir: str) -> str:
    """確定済みのLINE回答を修復プロンプト用の知識ブロックにする。

    序盤の質問への回答（正しい社名・教材名・数値など）は、後半に残る
    同種の崩れを推測可能にする。担当者の回答は最優先の事実として渡す。
    """
    path = os.path.join(job_dir, "unknown_points.json")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(data, list):
        return ""
    lines: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").strip().lower()
        if status not in {"answered", "resolved", "done", "closed"}:
            continue
        answer = str(item.get("answer") or "").strip()
        topic = str(
            item.get("text") or item.get("anomaly_word") or ""
        ).strip()
        if not answer or not topic:
            continue
        lines.append(f"- 確認事項: {topic[:100]} → 回答: {answer[:160]}")
        if len(lines) >= ANSWERED_KNOWLEDGE_MAX_ITEMS:
            break
    if not lines:
        return ""
    return "【担当者が確定済みの回答（最優先の事実として尊重する）】\n" + "\n".join(
        lines
    )


def _repair_dense_paragraphs(
    text: str,
    unresolved_findings: list[dict[str, Any]],
    meeting_profile: dict[str, Any] | None,
    extra_knowledge: str = "",
) -> tuple[str, list[dict[str, Any]]]:
    """Rewrite only paragraphs where reader-blocking garbles cluster.

    スパン置換で直せない密集した崩れ段落だけを、事実検証付きで段落単位に
    修復する。検証に落ちた段落は原文のまま残り、その問題は検証段の
    findings として質問に回る。
    """
    from editorial_transcript_pass import (
        _extract_response_text,
        _validate_editorial_paragraph,
        is_reader_blocking_finding,
        resolve_editorial_model,
    )

    paragraphs = _split_paragraphs(text)
    per_paragraph: dict[int, list[dict[str, Any]]] = {}
    for finding in unresolved_findings:
        if not is_reader_blocking_finding(finding):
            continue
        quote = str(finding.get("quote") or "").strip()
        if not quote:
            continue
        for index, paragraph in enumerate(paragraphs):
            if quote in paragraph:
                per_paragraph.setdefault(index, []).append(finding)
                break
    targets = {
        index: items
        for index, items in per_paragraph.items()
        if len(items) >= DENSE_PARAGRAPH_MIN_FINDINGS
    }
    if not targets:
        return text, []
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return text, []

    client = anthropic.Anthropic(api_key=api_key, timeout=DENSE_REPAIR_TIMEOUT_SEC)
    model = resolve_editorial_model()
    system = (
        "あなたは議事録の1段落だけを、読者が理解できる形に修復する編集者です。"
        "最優先は、後で読む人がストレスなく理解できることです。"
        "\n- 指摘された崩れ・意味不明箇所を、前後の文脈から確実に言える内容へ直す。"
        "\n- 正確に復元できない低価値の崩れ片は削除する。内容上必要だが細部だけ"
        "不明なら、確実に言える範囲へ一般化する。"
        "\n- 人名・数値・固有名詞は一つも変更・削除・追加しない。"
        "新しい事実・推測の固有名詞を作らない。"
        "\n- 出力は修復後の段落本文のみ。注釈・前置き・要約は禁止。"
    )
    if extra_knowledge.strip():
        system = system + "\n\n" + extra_knowledge.strip()

    def repair_one(
        index: int,
        paragraph: str,
        issues: list[dict[str, Any]],
    ) -> tuple[int, str | None]:
        payload = {
            "paragraph": paragraph,
            "issues": [
                {
                    "quote": str(f.get("quote") or "")[:120],
                    "issue": str(f.get("issue") or "")[:200],
                }
                for f in issues
            ],
        }
        try:
            response = client.messages.create(
                model=model,
                max_tokens=4000,
                system=system,
                messages=[
                    {
                        "role": "user",
                        "content": json.dumps(payload, ensure_ascii=False),
                    }
                ],
            )
            candidate = _extract_response_text(response)
        except Exception as exc:  # noqa: BLE001
            print(f"dense_repair_request_failed index={index} error={exc!r}")
            return index, None
        if not candidate.strip():
            return index, None
        errors = _validate_editorial_paragraph(
            paragraph,
            candidate,
            meeting_profile,
        )
        if errors:
            print(f"dense_repair_rejected index={index} errors={errors}")
            return index, None
        return index, candidate

    applied: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=DENSE_REPAIR_MAX_PARALLEL
    ) as executor:
        futures = [
            executor.submit(repair_one, index, paragraphs[index], items)
            for index, items in targets.items()
        ]
        for future in concurrent.futures.as_completed(futures):
            index, candidate = future.result()
            if candidate is None:
                continue
            paragraphs[index] = candidate
            applied.append(
                {
                    "type": "dense_paragraph_repair",
                    "quote": "\n".join(
                        str(f.get("quote") or "") for f in targets[index]
                    )[:200],
                    "fix": candidate[:200],
                    "confidence": "high",
                    "paragraph_index": index,
                }
            )
    if not applied:
        return text, []
    return "\n\n".join(paragraphs) + "\n", applied


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
    # 確定済み回答のカスケード: 回答が反映されるたびに再実行されるこのパスで、
    # 序盤の回答知識を使って後半の残存崩れを解決できるようにする。
    answered_knowledge = _answered_knowledge_block(job_dir)
    stats["answered_knowledge_items"] = answered_knowledge.count("\n- ")

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
                    extra_knowledge=answered_knowledge,
                )
            )
            applied.extend(resolved)
            skipped.extend(resolver_skipped)
        except Exception as exc:  # noqa: BLE001
            print(f"unified_resolver_failed={exc!r}")
        # 崩れが集中して残った段落は段落単位で修復（事実検証付き）。
        applied_quotes = {str(f.get("quote") or "") for f in applied}
        still_unresolved = [
            f
            for f in findings
            if str(f.get("quote") or "")
            and str(f.get("quote") or "") not in applied_quotes
        ]
        try:
            text_out, dense_applied = _repair_dense_paragraphs(
                text_out,
                still_unresolved,
                profile,
                extra_knowledge=answered_knowledge,
            )
            applied.extend(dense_applied)
        except Exception as exc:  # noqa: BLE001
            print(f"unified_dense_repair_failed={exc!r}")
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
        payload = json.dumps(
            {"stats": stats, "report": report},
            ensure_ascii=False,
            indent=2,
        )
        path = os.path.join(job_dir, UNIFIED_REPORT_FILENAME)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(payload)
        # 再実行で上書きされると「何が検出されどのゲートで却下されたか」の
        # 証跡が消える。実行ごとのアーカイブを必ず残す。
        stamp = time.strftime("%Y%m%d_%H%M%S")
        archive = os.path.join(
            job_dir, f"unified_finishing_report.{stamp}.json"
        )
        with open(archive, "w", encoding="utf-8") as handle:
            handle.write(payload)
    except OSError as exc:
        print(f"unified_report_write_failed={exc!r}")
    print(
        "unified_finishing_done "
        f"windows={window_count} audit_findings={len(audit_findings)} "
        f"applied={len(report['applied'])} remaining={len(remaining)} "
        f"duration_sec={stats['duration_sec']}"
    )
    return out_text, stats, report
