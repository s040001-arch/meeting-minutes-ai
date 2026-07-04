#!/usr/bin/env python3
"""Reader pass: Opus 4.8 で逐語録の論旨不明箇所を抽出する。"""
from __future__ import annotations

import json
import pathlib
import sys

import anthropic
from dotenv import load_dotenv

load_dotenv()

ROOT = pathlib.Path(__file__).resolve().parents[1]

TRANSCRIPT = ROOT / "scripts/fixtures/job_20260701_053826_mechanical.txt"
OUT = ROOT / "scripts/fixtures/reader_pass_20260701_result.json"

PROMPT = (
    "あなたはこの会議に出ていない読者です。この逐語録を読み、"
    "論旨が追えない・意味が通らない箇所を、理解を妨げる度合いが大きい順に列挙してください。"
    "各箇所について: (1)該当テキスト (2)なぜ分からないか (3)確認するならどんな質問が有効か。"
    "口語の癖や些末な言い淀みは挙げない。"
    "会議の内容把握に支障がある箇所だけ。上位10箇所まで。"
)


def main() -> int:
    transcript = TRANSCRIPT.read_text(encoding="utf-8")

    client = anthropic.Anthropic()
    message = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=4096,
        messages=[
            {
                "role": "user",
                "content": f"# 逐語録\n\n{transcript}\n\n---\n\n{PROMPT}",
            }
        ],
    )

    raw_text = message.content[0].text

    result = {
        "model": message.model,
        "input_tokens": message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
        "stop_reason": message.stop_reason,
        "transcript_source": str(TRANSCRIPT.relative_to(ROOT)),
        "findings_text": raw_text,
    }

    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUT}")
    print(f"tokens: in={result['input_tokens']} out={result['output_tokens']}")
    print()
    print(raw_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
