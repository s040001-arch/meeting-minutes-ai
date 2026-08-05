#!/usr/bin/env python3
"""楽天ジョブの質問重複状況を調べる。"""
from __future__ import annotations

import glob
import json
import os

job_dir = glob.glob("/app/data/transcriptions/job_20260804_140938*")[0]


def load(name):
    path = os.path.join(job_dir, name)
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


unknowns = load("unknown_points.json") or []
print("=== unknown_points:", len(unknowns))
for item in unknowns:
    print(
        json.dumps(
            {
                "status": item.get("status"),
                "source": item.get("source"),
                "type": item.get("type"),
                "text": str(item.get("text") or "")[:60],
                "answer": str(item.get("answer") or "")[:40],
                "id": str(item.get("anomaly_id") or "")[:20],
            },
            ensure_ascii=False,
        )
    )

asked = load("asked_questions.json")
if isinstance(asked, list):
    print("=== asked_questions:", len(asked))
    for q in asked[-12:]:
        if isinstance(q, dict):
            print(
                json.dumps(
                    {
                        "at": str(q.get("asked_at") or q.get("at") or "")[:19],
                        "q": str(
                            q.get("question_text") or q.get("text") or ""
                        )[:80],
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print(str(q)[:80])
