#!/usr/bin/env python3
"""THR: 「ロー点ツリー」→「論点ツリー」を after_qa に反映し、Doc設定を確認。"""
from __future__ import annotations

import glob
import json
import os
from datetime import datetime

job_dir = glob.glob("/app/data/transcriptions/job_20260730_025256*")[0]
path = os.path.join(job_dir, "merged_transcript_after_qa.txt")
with open(path, encoding="utf-8") as handle:
    text = handle.read()
count = text.count("ロー点ツリー")
if count:
    text = text.replace("ロー点ツリー", "論点ツリー")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    with open(
        os.path.join(job_dir, "line_correction_audit.jsonl"), "a", encoding="utf-8"
    ) as handle:
        handle.write(
            json.dumps(
                {
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "wrong": "ロー点ツリー",
                    "correct": "論点ツリー",
                    "applied_count": count,
                    "status": "applied_high_confidence_review_consensus",
                },
                ensure_ascii=False,
            )
            + "\n"
        )
print("ronten_replaced:", count)

hub_path = os.path.join(job_dir, "google_doc_hub.json")
if os.path.isfile(hub_path):
    with open(hub_path, encoding="utf-8") as handle:
        print("hub:", json.dumps(json.load(handle), ensure_ascii=False)[:300])
else:
    print("hub: MISSING")
    print("files:", sorted(os.listdir(job_dir))[:40])
