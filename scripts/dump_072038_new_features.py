#!/usr/bin/env python3
"""job_072038 の新機能（事前情報・参加者名正規化）の動作内容をダンプする。"""
from __future__ import annotations

import glob
import json
import os

job_dir = sorted(
    glob.glob("/app/data/transcriptions/job_*"), key=os.path.getmtime, reverse=True
)[0]
print(f"job={os.path.basename(job_dir)}")

print("\n=== participant_normalization_audit.json ===")
p = os.path.join(job_dir, "participant_normalization_audit.json")
if os.path.isfile(p):
    print(open(p, encoding="utf-8").read())

print("=== meeting_profile relevant_knowledge ===")
with open(os.path.join(job_dir, "meeting_profile.json"), encoding="utf-8") as f:
    prof = json.load(f)
for m in prof.get("relevant_knowledge") or []:
    print(f"- {m}")

print("\n=== prior_context.txt (先頭300字) ===")
pc = os.path.join(job_dir, "prior_context.txt")
if os.path.isfile(pc):
    print(open(pc, encoding="utf-8").read()[:300])

print("\n=== processing_visible_log.txt ===")
print(open(os.path.join(job_dir, "processing_visible_log.txt"), encoding="utf-8").read())

print("=== progress.json ===")
with open(os.path.join(job_dir, "progress.json"), encoding="utf-8") as f:
    prog = json.load(f)
print(json.dumps(prog, ensure_ascii=False)[:800])
