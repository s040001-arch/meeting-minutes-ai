#!/usr/bin/env python3
"""job_072038 の reader pass 結果と山谷の残存状況を確認する。"""
from __future__ import annotations

import glob
import json
import os

job_dir = sorted(
    glob.glob("/app/data/transcriptions/job_*"), key=os.path.getmtime, reverse=True
)[0]
print(f"job={os.path.basename(job_dir)}")

ai_path = os.path.join(job_dir, "merged_transcript_ai.txt")
text = open(ai_path, encoding="utf-8").read()
for token in ["山谷", "山家", "山屋", "山さん"]:
    print(f"token {token}: {text.count(token)}")

print("\n=== processing_visible_log.txt ===")
print(open(os.path.join(job_dir, "processing_visible_log.txt"), encoding="utf-8").read())

print("=== reader_pass_questions.md ===")
p = os.path.join(job_dir, "reader_pass_questions.md")
if os.path.isfile(p):
    print(open(p, encoding="utf-8").read()[:2500])
