"""発言録の分節サマリ見出しを生成する。

長文の発言録(数万字)を斜め読みできるよう、~2,500字単位の塊ごとに
「▼○○○○」形式の見出しを差し込む。見出しは Markdown の H3 (`### ▼xxx`)
として書き出し、Google Docs export 側で HEADING_3 (太字) スタイルが適用される。

非致命: ANTHROPIC_API_KEY 未設定や Claude エラー時は原文をそのまま返す。
"""
from __future__ import annotations

import concurrent.futures
import os
import re
from typing import Any

import anthropic

from meeting_profile import format_meeting_profile_for_prompt

# 目安: 1分速約 250-300字、つまり 2,500字は 8-10 分の会話塊。
# 1 時間ミーティングなら 6-8 セクション、3 時間なら 18-24 セクションに分かれる。
SECTION_TARGET_CHARS = 2500
SECTION_MIN_CHARS = 1500  # これ未満の末尾断片は直前セクションに併合する
SUMMARY_MODEL = "claude-sonnet-4-20250514"
SUMMARY_MAX_TOKENS = 200
SUMMARY_TIMEOUT_SEC = 120
MAX_PARALLEL = 4

SUMMARY_PREFIX = "▼"
HEADING_PREFIX = "### "  # Markdown H3, export 側で HEADING_3 にマップ


_PARAGRAPH_SEP = re.compile(r"\n\s*\n+")


def split_into_sections(
    text: str,
    target_chars: int = SECTION_TARGET_CHARS,
    min_chars: int = SECTION_MIN_CHARS,
) -> list[str]:
    """発言録を段落境界で ~target_chars ずつ分節する。

    - 段落の途中で切らない(可読性を維持)
    - 末尾の極小断片は直前セクションに併合(孤立した1-2段落の見出しを避ける)
    - 結果が1セクションしか作られない場合は呼び出し側で見出し付与をスキップ可能
    """
    paragraphs = [p.strip() for p in _PARAGRAPH_SEP.split(text.strip()) if p.strip()]
    if not paragraphs:
        return []

    sections: list[list[str]] = []
    current: list[str] = []
    current_chars = 0
    for p in paragraphs:
        current.append(p)
        current_chars += len(p)
        if current_chars >= target_chars:
            sections.append(current)
            current = []
            current_chars = 0
    if current:
        # 残りが短すぎる場合は直前セクションに併合
        tail_chars = sum(len(p) for p in current)
        if sections and tail_chars < min_chars // 2:
            sections[-1].extend(current)
        else:
            sections.append(current)

    return ["\n\n".join(sec) for sec in sections]


def _build_summary_system_prompt(meeting_profile: dict[str, Any] | None) -> str:
    profile_block = format_meeting_profile_for_prompt(meeting_profile or {})
    return (
        "あなたは議事録の発言録分節サマリ担当です。"
        "渡された発言録の一部(数分間の会話塊)を読み、"
        "この塊で何が話されていたかを15〜30字の見出し1行で返してください。"
        + profile_block
        + "\n\n【出力ルール】"
        "\n- 名詞句中心、体言止め推奨(動詞は最小限)"
        "\n- 例: 「シニア研修の対象層と検討アプローチ」"
        "\n- 「〜について」「〜の話」「〜に関して」のような冗長な接尾辞は避ける"
        "\n- 文末に句点を付けない、引用符を付けない"
        "\n- 出力は見出しテキスト1行のみ。前置き・コードフェンス・説明文を一切付けない"
        "\n- 議事録読者(相原)が後で『この塊はこんな話だった』と思い出せる粒度で書く"
    )


def _clean_summary_line(raw: str) -> str:
    """Claude 出力から見出し1行を取り出して整える。"""
    if not raw:
        return ""
    first_line = ""
    for line in raw.splitlines():
        s = line.strip()
        if s:
            first_line = s
            break
    if not first_line:
        return ""
    # よくある余計な装飾を除去
    cleaned = first_line.strip()
    cleaned = cleaned.lstrip("#").strip()
    cleaned = cleaned.strip("「」『』\"'`")
    cleaned = re.sub(r"^[▼▶■◆●・\-\*]+\s*", "", cleaned)
    cleaned = cleaned.rstrip("。．.,、")
    if not cleaned:
        return ""
    # 長さ制限(安全策)
    if len(cleaned) > 60:
        cleaned = cleaned[:59].rstrip() + "…"
    return cleaned


def _summarize_one_section(
    client: anthropic.Anthropic,
    section_text: str,
    system_prompt: str,
) -> str:
    resp = client.messages.create(
        model=SUMMARY_MODEL,
        max_tokens=SUMMARY_MAX_TOKENS,
        temperature=0,
        timeout=SUMMARY_TIMEOUT_SEC,
        system=system_prompt,
        messages=[{"role": "user", "content": section_text}],
    )
    parts: list[str] = []
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", "") == "text":
            parts.append(str(getattr(block, "text", "") or ""))
    raw = "\n".join(p for p in parts if p)
    return _clean_summary_line(raw)


def _summarize_sections_parallel(
    sections: list[str],
    meeting_profile: dict[str, Any] | None,
) -> list[str]:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return ["" for _ in sections]
    client = anthropic.Anthropic(api_key=api_key)
    system_prompt = _build_summary_system_prompt(meeting_profile)
    results: list[str] = [""] * len(sections)
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_PARALLEL) as executor:
        future_to_idx = {
            executor.submit(_summarize_one_section, client, sec, system_prompt): i
            for i, sec in enumerate(sections)
        }
        for future in concurrent.futures.as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                print(f"section_summary_failed idx={idx} error={e!r}")
                results[idx] = ""
    return results


def add_section_headings(
    text: str,
    meeting_profile: dict[str, Any] | None = None,
    *,
    target_chars: int = SECTION_TARGET_CHARS,
    min_chars: int = SECTION_MIN_CHARS,
) -> str:
    """発言録に分節サマリ見出しを付与した markdown を返す。

    1セクションしか作られない短い発言録、API キー未設定、全要約失敗の
    いずれかの場合は元のテキストをそのまま返す(見出し未付与)。
    """
    sections = split_into_sections(text, target_chars=target_chars, min_chars=min_chars)
    if len(sections) <= 1:
        return text.strip() + "\n"

    summaries = _summarize_sections_parallel(sections, meeting_profile)
    # 全要約失敗時は原文返却(差分なしで安全)
    if all(not s for s in summaries):
        return text.strip() + "\n"

    out_parts: list[str] = []
    for idx, (sec, summary) in enumerate(zip(sections, summaries), start=1):
        if summary:
            heading = f"{HEADING_PREFIX}{SUMMARY_PREFIX}{summary}"
        else:
            heading = f"{HEADING_PREFIX}{SUMMARY_PREFIX}（パート{idx}）"
        out_parts.append(f"{heading}\n\n{sec}")
    return "\n\n".join(out_parts) + "\n"


def main() -> int:
    """CLI: 単体実行で手元のテキストに見出しを付ける(デバッグ用)。"""
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(
        description="発言録テキストに分節サマリ見出しを付与する"
    )
    parser.add_argument("--input", required=True, help="入力テキストファイル")
    parser.add_argument(
        "--output",
        default=None,
        help="出力先(未指定時は標準出力)",
    )
    parser.add_argument(
        "--meeting-profile-json",
        default=None,
        help="meeting_profile.json のパス(任意、サマリ品質向上用)",
    )
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        text = f.read()
    profile = None
    if args.meeting_profile_json and os.path.isfile(args.meeting_profile_json):
        with open(args.meeting_profile_json, "r", encoding="utf-8") as f:
            profile = json.load(f)
    result = add_section_headings(text, profile)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(result)
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
