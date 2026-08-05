#!/usr/bin/env python3
"""job_072038 の回答反映結果と次の質問を確認する。"""
from __future__ import annotations

import glob
import json
import os

job_dir = sorted(
    glob.glob("/app/data/transcriptions/job_*"), key=os.path.getmtime, reverse=True
)[0]
print(f"job={os.path.basename(job_dir)}")

text = open(
    os.path.join(job_dir, "merged_transcript_after_qa.txt"), encoding="utf-8"
).read()
for token in ["山谷", "謝さん", "山屋", "メンタル", "メーター", "琵琶湖", "習字"]:
    print(f"token {token}: {text.count(token)}")

print("\n=== 現在の質問 (question_message.txt) ===")
print(open(os.path.join(job_dir, "question_message.txt"), encoding="utf-8").read())

print("\n=== answer_light_log.txt ===")
print(open(os.path.join(job_dir, "answer_light_log.txt"), encoding="utf-8").read())
