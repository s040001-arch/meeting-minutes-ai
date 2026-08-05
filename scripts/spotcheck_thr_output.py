#!/usr/bin/env python3
"""THR統合仕上げ出力のスポットチェック。"""
from __future__ import annotations

import glob
import os

job_dir = glob.glob("/app/data/transcriptions/job_20260730_025256*")[0]
path = os.path.join(job_dir, "merged_transcript_readable.txt")
with open(path, encoding="utf-8") as handle:
    text = handle.read()

print("chars:", len(text))
garbles = [
    "ゴリラ文化指導",
    "月マネジメント",
    "神ゲーム",
    "少し女使って",
    "宣伝にクロード",
    "遺伝性をする",
    "また帰れてる",
    "ロー点ツリー",
    "山谷",
    "メモの部長",
]
for token in garbles:
    print("garble_remains" if token in text else "fixed_or_absent", "|", token)

# 周辺文脈の確認（修正結果の読みやすさ）
for probe in ("文化指導", "クロード", "KPIツリー"):
    idx = text.find(probe)
    if idx >= 0:
        print("---", probe, ":", text[max(0, idx - 60) : idx + 80].replace("\n", " "))
