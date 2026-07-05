#!/usr/bin/env python3
"""thrジョブの after_qa から Box節の崩壊文の現在文面を前後広めに出力する。"""
from __future__ import annotations

import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

JOB_ID = "job_20260705_055804_2026_0624_thr社_運営改善_西脇様_竹中様_福田_相原"
PATH = Path("data/transcriptions") / JOB_ID / "merged_transcript_after_qa.txt"

ANCHORS = [
    "ドネと協定",
    "日系である",
    "ホームベース",
    "何月とかには住んでいる",
    "何月かに住んでいる",
    "レンズさん",
    "魔王社",
    "はめるのを伝え",
    "カフェですか",
    "精神とボックス",
    "Box",
]


def main() -> int:
    text = PATH.read_text(encoding="utf-8")
    print(f"len={len(text)}")
    for word in ANCHORS:
        idx = 0
        hits = 0
        while hits < 4:
            i = text.find(word, idx)
            if i < 0:
                break
            hits += 1
            s = max(0, i - 120)
            e = min(len(text), i + len(word) + 120)
            snippet = text[s:e].replace("\n", "\\n")
            print(f"\n[{word}] @{i}:\n...{snippet}...")
            idx = i + 1
        if hits == 0:
            print(f"\n[{word}] not found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
