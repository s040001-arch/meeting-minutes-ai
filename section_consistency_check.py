#!/usr/bin/env python3
"""セクション整合性チェック（2026-08-07）。

背景: 6.2 は要約・決定事項・残論点を1回のLLM生成で作り、生成後の
検証がなかった。その結果「N対Nがいいかもしれない」「3対3とかいいですね」
という検討段階の会話が決定事項に断定で書かれ、残論点との矛盾も
検出されないまま公開された（楽天ジョブの実害）。

このモジュールは生成直後に決定論的な後段検証を行う:
1. 決定事項の各項目について、発言録に「明示的な合意」の根拠があるかを
   LLM に引用付きで判定させる。
2. 根拠が弱い項目（提案止まり・片方の同調のみ）は残論点へ降格する。
   降格は削除ではないため情報は失われない（安全側は「断定しない」）。
3. 決定事項と残論点で同じ論点が両方に載っている矛盾を検出し、
   決定側を降格して一本化する。
LLM 呼び出しが失敗した場合はフェイルオープン（無修正＋報告のみ）。
判定の誤りが起きても「決定→検討中」方向にしか動かないため、
事実の捏造方向のリスクはない。
"""
from __future__ import annotations

import json
import os
from typing import Any

import anthropic

_MODEL = os.environ.get("SECTION_CONSISTENCY_MODEL", "claude-sonnet-5")
_TIMEOUT_SEC = 180
REPORT_FILENAME = "section_consistency_report.json"

_SYSTEM = (
    "あなたは議事録の監査担当です。決定事項の各項目について、発言録に"
    "『明示的な合意』の根拠があるかを判定します。"
    "\n- explicit: 双方の合意が発言から読み取れる（「じゃあそれで」「決定」"
    "「それで行きましょう」「はい、お願いします」等の応答がある）。"
    "\n- tentative: 提案・案・「いいですね」「いいかもしれない」等の"
    "前向き反応止まりで、最終合意の発言がない。"
    "\n- none: 発言録に対応する内容が見つからない。"
    "\nまた、open_issues（残論点）と同じ論点を扱っていて矛盾する場合は"
    "その残論点の index を conflict に入れます（なければ null）。"
    "\n出力は JSON 配列のみ: "
    '[{"index":0,"evidence":"explicit","quote":"根拠となる発言の引用",'
    '"conflict":null}]'
)


def _extract_json_array(text: str) -> list[Any]:
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("no json array in response")
    return json.loads(text[start : end + 1])


def check_and_fix_sections(
    sections: dict[str, Any],
    transcript_md: str,
    *,
    job_dir: str | None = None,
    model: str = _MODEL,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(修正後sections, 報告) を返す。失敗時は無修正で返す。"""
    report: dict[str, Any] = {
        "attempted": False,
        "demoted": [],
        "conflicts": [],
        "error": None,
        "model": model,
    }
    decisions = [str(x) for x in (sections.get("decisions") or [])]
    open_issues = [str(x) for x in (sections.get("open_issues") or [])]
    if not decisions:
        return sections, report
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        report["error"] = "anthropic_api_key_missing"
        return sections, report
    report["attempted"] = True
    payload = json.dumps(
        {
            "decisions": decisions,
            "open_issues": open_issues,
            "transcript": transcript_md,
        },
        ensure_ascii=False,
    )
    try:
        client = anthropic.Anthropic(api_key=api_key, timeout=_TIMEOUT_SEC)
        response = client.messages.create(
            model=model,
            max_tokens=3000,
            system=_SYSTEM,
            messages=[{"role": "user", "content": payload}],
        )
        parts = [
            getattr(block, "text", "") for block in (response.content or [])
        ]
        rows = _extract_json_array("".join(parts))
    except Exception as exc:  # noqa: BLE001
        report["error"] = f"consistency_llm_failed:{exc!r}"
        _write_report(job_dir, report)
        return sections, report

    verdict_by_index: dict[int, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            index = int(row.get("index"))
        except (TypeError, ValueError):
            continue
        verdict_by_index[index] = row

    # 2026-08-07 GPT監査指摘: LLM応答に欠けた index を無言で explicit 扱い
    # すると未検証の決定が素通りする。全 index の判定が揃わない場合は
    # 技術的失敗として無修正で返す（判定なし＝根拠あり、とはしない）。
    missing = [i for i in range(len(decisions)) if i not in verdict_by_index]
    if missing:
        report["error"] = f"consistency_incomplete_rows:missing={missing}"
        _write_report(job_dir, report)
        return sections, report

    kept: list[str] = []
    new_open: list[str] = list(open_issues)
    for index, decision in enumerate(decisions):
        row = verdict_by_index.get(index) or {}
        evidence = str(row.get("evidence") or "explicit").strip().lower()
        conflict = row.get("conflict")
        if evidence == "explicit" and conflict is None:
            kept.append(decision)
            continue
        # 降格: 断定を外して残論点へ。矛盾時は残論点側の記述を優先し、
        # 決定側は消さずに「有力案」として合流させる。
        demoted = f"{decision}（会話上は有力案の段階で、最終確定は未確認）"
        if conflict is not None:
            report["conflicts"].append(
                {
                    "decision": decision,
                    "open_issue_index": conflict,
                    "quote": str(row.get("quote") or ""),
                }
            )
        report["demoted"].append(
            {
                "decision": decision,
                "evidence": evidence,
                "quote": str(row.get("quote") or ""),
            }
        )
        new_open.append(demoted)

    if report["demoted"]:
        sections = dict(sections)
        sections["decisions"] = kept
        sections["open_issues"] = new_open
    _write_report(job_dir, report)
    return sections, report


def _write_report(job_dir: str | None, report: dict[str, Any]) -> None:
    if not job_dir:
        return
    try:
        path = os.path.join(job_dir, REPORT_FILENAME)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
    except OSError as exc:
        print(f"section_consistency_report_write_failed={exc!r}")
