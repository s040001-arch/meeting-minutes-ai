"""Independent cross-family verifier for the single-pass transcript editor."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import requests

from ai_correct_text import resolve_openai_api_key
from shadow_single_pass_editor import _job_answer_context


DEFAULT_MODEL = os.environ.get(
    "SINGLE_PASS_VERIFIER_MODEL", "gpt-5.6-sol"
).strip()
REPORT_FILENAME = "single_pass_verifier_report.json"
_QUESTION_BLOCKING_TYPES = {
    "fact_shift",
    "unsupported_addition",
    "important_omission",
    "unreadable",
    "back_half_degradation",
}

_SYSTEM = """\
あなたは、会話調の整文記録を公開する前の独立監査人です。
編集者とは別系統のモデルとして、文章を良く書き直すのではなく、次だけを検査します。

1. 人名・会社名・製品名・数値・金額・日時・比率・肯定否定・発言主体・決定内容の変化
2. 原文やユーザー確定回答に根拠がない具体情報の追加
3. 重要な理由・条件・具体例・反対意見・結論の脱落
4. 初見の読者が意味を取れない音声認識崩れの残存
5. 文書後半だけ品質が低下していないか

ユーザー確定回答は原文より優先します。
フィラー・相槌・重複・言い直し残骸の削除、接続や主語の穏当な補完は問題にしません。
単なる文体の好みは報告しません。
同じ問題は1件にまとめます。

出力はJSONオブジェクトのみ:
{
  "status":"pass|blocked",
  "findings":[{
    "severity":"blocker|warning",
    "type":"fact_shift|unsupported_addition|important_omission|unreadable|back_half_degradation",
    "raw_quote":"原文引用",
    "edited_quote":"整文稿の完全一致引用。該当しなければ空",
    "issue":"問題の説明",
    "question_needed":true|false,
    "hypothesis":"質問時に提示できる候補。なければ空",
    "replacement":"質問不要で原文・確定回答から一意に直せる場合だけ、edited_quote全体の置換後。その他は空"
  }],
  "summary":"短い総括"
}

blockerは、公開すると事実または理解を誤らせる問題だけです。
warningは軽微で公開可能なものです。
"""


def _extract_output_text(payload: dict[str, Any]) -> str:
    texts: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if (
                isinstance(content, dict)
                and content.get("type") == "output_text"
            ):
                texts.append(str(content.get("text") or ""))
    return "\n".join(texts).strip()


def _parse_json_object(text: str) -> dict[str, Any]:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("verifier output is not an object")
    return value


def verify_single_pass_transcript(
    *,
    raw_text: str,
    edited_text: str,
    job_dir: Path,
    model: str = DEFAULT_MODEL,
    timeout_sec: int = 600,
) -> dict[str, Any]:
    api_key, key_source = resolve_openai_api_key()
    if not api_key:
        return {
            "status": "blocked",
            "findings": [
                {
                    "severity": "blocker",
                    "type": "verifier_error",
                    "raw_quote": "",
                    "edited_quote": "",
                    "issue": "OPENAI_API_KEYがなく独立監査を実行できない",
                    "question_needed": False,
                    "hypothesis": "",
                }
            ],
            "summary": "独立監査未実施",
            "error": "openai_api_key_missing",
        }
    user_payload = {
        "raw_transcript": raw_text,
        "human_confirmed_answers": _job_answer_context(job_dir),
        "edited_transcript": edited_text,
    }
    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "max_output_tokens": 6000,
            "input": [
                {"role": "system", "content": _SYSTEM},
                {
                    "role": "user",
                    "content": json.dumps(user_payload, ensure_ascii=False),
                },
            ],
        },
        timeout=timeout_sec,
    )
    if response.status_code != 200:
        raise RuntimeError(
            "OpenAI verifier error: "
            f"status={response.status_code} body={response.text[:500]}"
        )
    report = _parse_json_object(_extract_output_text(response.json()))
    findings = [
        item
        for item in (report.get("findings") or [])
        if isinstance(item, dict)
    ]
    for item in findings:
        if (
            bool(item.get("question_needed"))
            and str(item.get("type") or "").strip().lower()
            in _QUESTION_BLOCKING_TYPES
        ):
            # A model may call an unreadable factual ambiguity a warning while
            # simultaneously saying the user must answer it.  The latter is
            # authoritative for publication safety.
            item["severity"] = "blocker"
    report["findings"] = findings
    report["status"] = (
        "blocked"
        if any(
            str(item.get("severity") or "").lower() == "blocker"
            for item in findings
        )
        else "pass"
    )
    report["model"] = model
    report["api_key_source"] = key_source
    report["raw_chars"] = len(raw_text)
    report["edited_chars"] = len(edited_text)
    return report


def write_verifier_report(job_dir: Path, report: dict[str, Any]) -> Path:
    path = job_dir / REPORT_FILENAME
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def apply_deterministic_verifier_repairs(
    edited_text: str,
    report: dict[str, Any],
) -> tuple[str, list[dict[str, str]]]:
    """Apply exact, non-question replacements supplied by the verifier.

    Warning-level corrections are included when the verifier says no user
    question is needed.  The edited quote must occur exactly once.  No fuzzy
    or global replacement is allowed.
    """
    output = edited_text
    applied: list[dict[str, str]] = []
    for finding in report.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        if bool(finding.get("question_needed", True)):
            continue
        before = str(finding.get("edited_quote") or "").strip()
        after = str(finding.get("replacement") or "").strip()
        if (
            not before
            or not after
            or before == after
            or output.count(before) != 1
        ):
            continue
        output = output.replace(before, after, 1)
        applied.append(
            {
                "before": before,
                "after": after,
                "issue": str(finding.get("issue") or ""),
            }
        )
    return output, applied


def verify_and_repair_until_stable(
    *,
    raw_text: str,
    edited_text: str,
    job_dir: Path,
    model: str = DEFAULT_MODEL,
    timeout_sec: int = 600,
    max_repair_rounds: int = 3,
) -> tuple[str, dict[str, Any], list[dict[str, str]]]:
    """Repeat exact repairs and independent verification to a fixed point."""
    current = edited_text
    reports: list[dict[str, Any]] = []
    all_repairs: list[dict[str, str]] = []
    final_report: dict[str, Any] = {}

    for _ in range(max_repair_rounds):
        report = verify_single_pass_transcript(
            raw_text=raw_text,
            edited_text=current,
            job_dir=job_dir,
            model=model,
            timeout_sec=timeout_sec,
        )
        reports.append(report)
        repaired, repairs = apply_deterministic_verifier_repairs(
            current, report
        )
        if not repairs:
            final_report = report
            break
        current = repaired
        all_repairs.extend(repairs)
    else:
        # The last loop iteration changed text, so audit that final text once
        # more.  No unaudited repair may reach publication.
        final_report = verify_single_pass_transcript(
            raw_text=raw_text,
            edited_text=current,
            job_dir=job_dir,
            model=model,
            timeout_sec=timeout_sec,
        )
        reports.append(final_report)

    final_report = dict(final_report)
    final_report["verification_rounds"] = reports
    final_report["deterministic_repairs"] = all_repairs
    return current, final_report, all_repairs


def verifier_findings_to_unknowns(
    report: dict[str, Any],
) -> list[dict[str, Any]]:
    """Convert only unresolved blockers into the existing LINE queue schema."""
    unknowns: list[dict[str, Any]] = []
    for finding in report.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        if str(finding.get("severity") or "").lower() != "blocker":
            continue
        quote = str(
            finding.get("edited_quote")
            or finding.get("raw_quote")
            or ""
        ).strip()
        issue = str(finding.get("issue") or "").strip()
        if not quote or not issue:
            continue
        unknowns.append(
            {
                "type": "fact_unknown",
                "text": issue,
                "proposal_impact": 10,
                "reason": "公開前の別系統AI監査で検出",
                "evidence": quote[:500],
                "hypothesis": str(
                    finding.get("hypothesis") or ""
                ).strip(),
                "source": "single_pass_independent_verifier",
                "anomaly_word": quote[:80],
                "span_text": quote,
                "status": "open",
                "question_needed": bool(
                    finding.get("question_needed", True)
                ),
            }
        )
    return unknowns
