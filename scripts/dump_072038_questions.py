#!/usr/bin/env python3
"""job_072038 の自動修正と送信済み質問を確認する。"""
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
for token in ["山谷", "山屋"]:
    print(f"token {token}: {text.count(token)}")

for name in ["auto_corrections.json", "correction_audit_log.json"]:
    p = os.path.join(job_dir, name)
    if os.path.isfile(p):
        print(f"\n=== {name} ===")
        print(open(p, encoding="utf-8").read())

print("\n=== question_message.txt ===")
print(open(os.path.join(job_dir, "question_message.txt"), encoding="utf-8").read())
