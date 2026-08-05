#!/usr/bin/env python3
"""送信済み質問と unknown_points の中身を確認する。"""
from __future__ import annotations

import glob
import json
import os

job_dir = glob.glob("/app/data/transcriptions/job_20260805_012909*")[0]

path = os.path.join(job_dir, "question_message.txt")
if os.path.isfile(path):
    with open(path, encoding="utf-8") as handle:
        print("=== question_message ===")
        print(handle.read())

with open(os.path.join(job_dir, "unknown_points.json"), encoding="utf-8") as f:
    points = json.load(f)
print("=== unknown_points ===")
for p in points:
    print(
        json.dumps(
            {
                "id": str(p.get("anomaly_id") or "")[:12],
                "word": str(p.get("anomaly_word") or p.get("text") or "")[:40],
                "status": p.get("status"),
                "source": p.get("source") or p.get("type"),
                "conf": p.get("confidence"),
            },
            ensure_ascii=False,
        )
    )
