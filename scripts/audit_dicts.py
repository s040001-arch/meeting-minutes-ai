#!/usr/bin/env python3
"""学習辞書と修正辞書の中身、および新ジョブへの適用状況を確認する。"""
from __future__ import annotations

import glob
import json
import os

for path in (
    "/app/data/knowledge/learned_corrections.json",
    "/app/data/correction_dict.json",
):
    print(f"--- {path} ---")
    if not os.path.isfile(path):
        print("(missing)")
        continue
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, list):
        print(f"entries={len(data)}")
        for entry in data[-10:]:
            print(json.dumps(entry, ensure_ascii=False)[:200])
    else:
        print(f"keys={len(data)}")
        print(json.dumps(data, ensure_ascii=False)[:1000])

job_dir = glob.glob("/app/data/transcriptions/job_20260805_012909*")[0]
with open(
    os.path.join(job_dir, "merged_transcript_mechanical.txt"),
    encoding="utf-8",
) as handle:
    mech = handle.read()
with open(
    os.path.join(job_dir, "merged_transcript.txt"), encoding="utf-8"
) as handle:
    raw = handle.read()
for token in ("Udemy", "ユーデミー", "山屋", "山家", "山谷", "俊也", "シニア", "シュニア"):
    print(f"count raw/mech {token}: {raw.count(token)}/{mech.count(token)}")

# 進行状況も添える
for name in ("progress.json",):
    path = os.path.join(job_dir, name)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as handle:
            print("progress:", handle.read()[:800])
