"""Reader pass: 会議に出ていない読者視点で逐語録の論旨不明箇所を抽出する。

タイミング: Step 4.3 (AI補正) 完了後、Step 6.2 (議事録整形) の前。
出力:
  {job_dir}/reader_pass_result.json   — findings 5件 (構造化)
  {job_dir}/reader_pass_questions.md  — 相原向け質問テンプレ (回答欄空)

制御: 環境変数 READER_PASS_ENABLED=on のときのみ走る (既定: off)。
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

import anthropic
from dotenv import load_dotenv

load_dotenv()

READER_PASS_MODEL = "claude-opus-4-8"
READER_PASS_RESULT = "reader_pass_result.json"
READER_PASS_QUESTIONS_MD = "reader_pass_questions.md"

_ANSWERED_STATUSES = {"answered", "done", "closed", "resolved", "ignored"}
_ASK_VERDICTS = {"ask_with_candidate", "ask_without_candidate"}

_PROMPT_TEMPLATE = """\
あなたはこの会議に出ていない読者です。以下の逐語録を読み、論旨が追えない・意味が通らない箇所を、\
理解を妨げる度合いが大きい順に上位5件列挙してください。

各件を以下のJSON配列形式のみで出力してください（コードブロック・前置き不要）:
[
  {{
    "rank": 1,
    "excerpt": "該当テキスト30字以内",
    "reason": "なぜ分からないか",
    "question": "確認するなら一問。回答は読者が文脈を理解できる完全な一文で。"
  }}
]

口語の癖・言い淀み・フィラーは挙げない。会議の内容把握に支障がある箇所だけ。
なお以下の箇所は既に確認予定なので除外してください:
{exclusion_list}"""


# ---------------------------------------------------------------------------
# 除外リスト構築
# ---------------------------------------------------------------------------

def load_exclusion_items(
    job_dir: Path,
    *,
    proposals_path: Path | None = None,
) -> list[str]:
    """edit_proposals.json の未回答②③ + unknown_points.json の open 項目を返す。"""
    items: list[str] = []

    ep_path = proposals_path or (job_dir / "edit_proposals.json")
    if ep_path.exists():
        try:
            doc = json.loads(ep_path.read_text(encoding="utf-8"))
            for p in doc.get("proposals") or []:
                if p.get("verdict") not in _ASK_VERDICTS:
                    continue
                word = str(p.get("anomaly_word") or p.get("span_before") or "").strip()
                if word:
                    items.append(word)
        except (OSError, json.JSONDecodeError):
            pass

    up_path = job_dir / "unknown_points.json"
    if up_path.exists():
        try:
            data = json.loads(up_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                for u in data:
                    if str(u.get("status") or "").strip().lower() in _ANSWERED_STATUSES:
                        continue
                    word = str(
                        u.get("anomaly_word") or u.get("text") or ""
                    ).strip()
                    if word:
                        items.append(word[:40])
        except (OSError, json.JSONDecodeError):
            pass

    return items


# ---------------------------------------------------------------------------
# LLM出力パーサ
# ---------------------------------------------------------------------------

def parse_findings(text: str) -> list[dict[str, Any]]:
    """LLM が返した JSON 文字列をパースして findings リストにする。
    JSON 抽出に失敗したときは空リストを返す（非致命的）。
    """
    # コードブロックを除去
    cleaned = re.sub(r"```(?:json)?\s*", "", text).strip()
    # 先頭の [ から末尾の ] までを抽出
    m = re.search(r"(\[.*\])", cleaned, re.DOTALL)
    if not m:
        return []
    try:
        findings = json.loads(m.group(1))
        if not isinstance(findings, list):
            return []
        return findings
    except json.JSONDecodeError:
        return []


# ---------------------------------------------------------------------------
# 質問MDテンプレ生成
# ---------------------------------------------------------------------------

def build_questions_md(findings: list[dict[str, Any]], job_id: str = "") -> str:
    header_suffix = f" — {job_id}" if job_id else ""
    lines = [
        f"# Reader Pass 確認シート{header_suffix}",
        "",
        "回答方法: 各「→ 回答:」欄に記入してください。",
        "",
        "---",
        "",
    ]
    for f in findings:
        rank = f.get("rank", "?")
        excerpt = f.get("excerpt", "")
        reason = f.get("reason", "")
        question = f.get("question", "")
        lines += [
            f"## #{rank}",
            "",
            "**該当テキスト**",
            f"> {excerpt}",
            "",
            "**なぜ分からないか**",
            reason,
            "",
            "**質問**",
            question,
            "",
            "→ 回答:",
            "",
            "---",
            "",
        ]
    lines.append(f"_reader_pass / {READER_PASS_MODEL}_")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# メイン実行関数
# ---------------------------------------------------------------------------

def run_reader_pass(
    job_dir: Path,
    *,
    ai_txt_path: Path | None = None,
    proposals_path: Path | None = None,
) -> dict[str, Any]:
    """Reader pass を実行し、結果 dict を返す。

    副作用:
      {job_dir}/reader_pass_result.json を書き込む
      {job_dir}/reader_pass_questions.md を書き込む
    """
    ai_path = ai_txt_path or (job_dir / "merged_transcript_ai.txt")
    transcript = ai_path.read_text(encoding="utf-8")

    exclusion_items = load_exclusion_items(job_dir, proposals_path=proposals_path)
    exclusion_list = (
        "\n".join(f"- {w}" for w in exclusion_items) if exclusion_items else "（なし）"
    )

    prompt = _PROMPT_TEMPLATE.format(exclusion_list=exclusion_list)
    user_message = f"# 逐語録\n\n{transcript}\n\n---\n\n{prompt}"

    client = anthropic.Anthropic()
    message = client.messages.create(
        model=READER_PASS_MODEL,
        max_tokens=2048,
        messages=[{"role": "user", "content": user_message}],
    )

    raw_text = message.content[0].text
    findings = parse_findings(raw_text)

    result: dict[str, Any] = {
        "model": message.model,
        "input_tokens": message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
        "stop_reason": message.stop_reason,
        "excluded_count": len(exclusion_items),
        "findings": findings,
        "findings_raw": raw_text,
    }

    (job_dir / READER_PASS_RESULT).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    job_id = job_dir.name
    md = build_questions_md(findings, job_id=job_id)
    (job_dir / READER_PASS_QUESTIONS_MD).write_text(md, encoding="utf-8")

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def is_enabled() -> bool:
    return os.environ.get("READER_PASS_ENABLED", "").strip().lower() == "on"


def main() -> int:
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Reader pass for a job directory")
    parser.add_argument("--job-dir", required=True, help="ジョブディレクトリ")
    parser.add_argument("--ai-txt", default=None, help="逐語録ファイルパス (省略時: job-dir/merged_transcript_ai.txt)")
    parser.add_argument("--proposals", default=None, help="edit_proposals.json パス (省略時: job-dir/edit_proposals.json)")
    parser.add_argument("--force", action="store_true", help="READER_PASS_ENABLED=off でも強制実行")
    args = parser.parse_args()

    if not args.force and not is_enabled():
        print("READER_PASS_ENABLED is not 'on'. Skip. (--force to override)")
        return 0

    job_dir = Path(args.job_dir)
    if not job_dir.is_dir():
        print(f"ERROR: job_dir not found: {job_dir}")
        return 1

    ai_txt_path = Path(args.ai_txt) if args.ai_txt else None
    proposals_path = Path(args.proposals) if args.proposals else None

    result = run_reader_pass(job_dir, ai_txt_path=ai_txt_path, proposals_path=proposals_path)

    print(f"wrote {job_dir / READER_PASS_RESULT}")
    print(f"wrote {job_dir / READER_PASS_QUESTIONS_MD}")
    print(f"model={result['model']}  in={result['input_tokens']}  out={result['output_tokens']}  excluded={result['excluded_count']}")
    print(f"findings={len(result['findings'])}件")
    print()
    for f in result["findings"]:
        print(f"  #{f.get('rank')}: {f.get('excerpt', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
