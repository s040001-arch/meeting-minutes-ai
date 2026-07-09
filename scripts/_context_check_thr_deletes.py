#!/usr/bin/env python3
"""delete 修復箇所の前後文脈を表示して文脈整合を確認する。"""
from __future__ import annotations

import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

JOB = Path("data/transcriptions") / (
    "job_20260705_055804_2026_0624_thr社_運営改善_西脇様_竹中様_福田_相原"
)
PATH = JOB / "merged_transcript_after_qa.txt"

ANCHORS = [
    ("冒頭・ご相談", "っていうところで、ご相談"),
    ("ベンダー・お付き合い", "お付き合いさせていただいてます"),
    ("実施要項・形式", "誰から見ても"),
    ("アンケート・共有", "本当に共有していこうか"),
]


def main() -> int:
    text = PATH.read_text(encoding="utf-8")
    for label, anchor in ANCHORS:
        i = text.find(anchor)
        if i < 0:
            print(f"\n=== {label} === NOT FOUND: {anchor!r}")
            continue
        s = max(0, i - 120)
        e = min(len(text), i + len(anchor) + 160)
        block = text[s:e].replace("\n", " ")
        print(f"\n=== {label} (@{i}) ===")
        print(block)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
