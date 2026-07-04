"""One-off: reconstruct the A-1 garbled span '記号を取ってる' via Opus.

Run after apply_rakuten_answers.py (which already stripped [補足:]).
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_TRANSCRIPT = _ROOT / "scripts" / "fixtures" / "job_20260701_053826_ai_with_notes.txt"

ANCHOR = "記号を取ってる"
CONFIRMED = "参加は任意の形式で良い"


def main() -> None:
    # Load .env
    env_path = _ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"\''))

    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    text = _TRANSCRIPT.read_text(encoding="utf-8")
    pos = text.find(ANCHOR)
    if pos < 0:
        print(f"ANCHOR not found: {ANCHOR!r}")
        sys.exit(1)

    # Extract ±2 sentences
    ends = "。？！\n"
    start = pos
    while start > 0 and text[start - 1] not in ends:
        start -= 1
    # one more sentence back
    if start > 0:
        prev = start - 1
        while prev > 0 and text[prev - 1] not in ends:
            prev -= 1
        start = prev

    end = pos + len(ANCHOR)
    count = 0
    while end < len(text) and count < 2:
        if text[end] in ends:
            count += 1
        end += 1

    span = text[start:end]
    print(f"Span to reconstruct:\n  {repr(span)}\n")

    try:
        import anthropic  # type: ignore
    except ImportError:
        print("anthropic not installed; skipping")
        sys.exit(0)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("No ANTHROPIC_API_KEY; skipping")
        sys.exit(0)

    client = anthropic.Anthropic(api_key=api_key)
    prompt = (
        "以下は日本語の会議逐語録の一部です。\n\n"
        f"【テキスト】\n{span}\n\n"
        f"【確定情報】\n{CONFIRMED}\n\n"
        "指示:\n"
        "- 「記号を取ってる——いや」は音声認識の崩れです。確定情報を参照して最小限修正してください。\n"
        "- 修正は「記号を取ってる——いや」の部分のみ。それ以外は一字一句変えないでください。\n"
        "- 数値・固有名詞・事実は変えないでください。\n"
        "- 修正後のテキストのみ出力してください（説明不要）。"
    )
    resp = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    result = ""
    for block in (resp.content or []):
        if getattr(block, "type", "") == "text":
            result += block.text
    result = result.strip()
    print(f"Opus output:\n  {repr(result)}\n")

    if not result or result == span:
        print("No change from Opus; leaving as-is.")
        return

    # Verify numbers preserved
    import re as _re
    orig_nums = _re.findall(r"\d+(?:[./]\d+)?", span)
    for n in orig_nums:
        if n not in result:
            print(f"Number lost: {n!r} → reverting")
            return

    # Apply
    new_text = text[:start] + result + text[end:]
    _TRANSCRIPT.write_text(new_text, encoding="utf-8")
    print(f"Applied. Chars: {len(text)} → {len(new_text)}")
    print(f"\nBEFORE: {repr(span)}")
    print(f"AFTER:  {repr(result)}")


if __name__ == "__main__":
    main()
