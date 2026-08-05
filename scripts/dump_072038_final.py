#!/usr/bin/env python3
"""job_072038 の品質ゲート判定と最終状態を確認する。"""
from __future__ import annotations

import glob
import json
import os

job_dir = sorted(
    glob.glob("/app/data/transcriptions/job_*"), key=os.path.getmtime, reverse=True
)[0]
print(f"job={os.path.basename(job_dir)}")

print("\n=== minutes_quality_gate.json ===")
print(open(os.path.join(job_dir, "minutes_quality_gate.json"), encoding="utf-8").read())

text = open(
    os.path.join(job_dir, "merged_transcript_readable.txt"), encoding="utf-8"
).read()
print("=== readable のトークン確認 ===")
for token in ["山谷", "山屋", "メンタルはここ", "習字", "琵琶湖", "湯でみ", "このデミ", "Udemy"]:
    print(f"token {token}: {text.count(token)}")

print("\n=== unified stats/remaining ===")
with open(os.path.join(job_dir, "unified_finishing_report.json"), encoding="utf-8") as f:
    rep = json.load(f)
print(json.dumps(rep.get("stats") or {}, ensure_ascii=False))
rem = rep.get("remaining_findings") or []
print(f"remaining={len(rem)}")
for r in rem[:10]:
    print("-", json.dumps(r, ensure_ascii=False)[:200])

print("\n=== question_message.txt ===")
print(open(os.path.join(job_dir, "question_message.txt"), encoding="utf-8").read())
