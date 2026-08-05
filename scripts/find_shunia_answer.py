#!/usr/bin/env python3
"""前回楽天ジョブの回答から「シュニア」の確定内容を探す。"""
from __future__ import annotations

import glob
import json
import os

for job_glob in ("job_20260804_140938*", "job_20260805_012909*"):
    for job_dir in glob.glob(f"/app/data/transcriptions/{job_glob}"):
        print(f"=== {os.path.basename(job_dir)[:40]} ===")
        for name in ("unknown_points.json", "answers.json", "asked_questions.json"):
            path = os.path.join(job_dir, name)
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as handle:
                raw = handle.read()
            if "シュニア" not in raw:
                continue
            data = json.loads(raw)
            items = data if isinstance(data, list) else [data]
            for item in items:
                blob = json.dumps(item, ensure_ascii=False)
                if "シュニア" in blob:
                    print(f"[{name}]", blob[:500])

# 新ジョブの該当箇所の文脈
job_dir = glob.glob("/app/data/transcriptions/job_20260805_012909*")[0]
with open(
    os.path.join(job_dir, "merged_transcript_readable.txt"), encoding="utf-8"
) as handle:
    text = handle.read()
pos = text.find("シュニア")
if pos >= 0:
    print("=== new context ===")
    print(text[max(0, pos - 150) : pos + 150])
