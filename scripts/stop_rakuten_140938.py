#!/usr/bin/env python3
"""楽天ジョブ(140938)を完全停止する。

- pause マーカーを残したまま overall_status を stopped_by_user に更新
- LINE の pending 質問コンテキストがこのジョブを指していれば解除
  （古い質問への返信で誤って再開しないようにする）
"""
from __future__ import annotations

import glob
import json
import os

job_dir = glob.glob("/app/data/transcriptions/job_20260804_140938*")[0]
job_id = os.path.basename(job_dir)

import sys

sys.path.insert(0, "/app")
from progress_tracker import update_job_progress  # noqa: E402

update_job_progress(
    input_root="/app/data/transcriptions",
    job_id=job_id,
    phase="stopped_by_user",
    status="success",
    detail={"reason": "user will resubmit the source text as a new job"},
    overall_status="stopped",
)
print("progress: stopped")

ctx_path = "/app/data/line_pending_context.json"
if os.path.isfile(ctx_path):
    try:
        with open(ctx_path, encoding="utf-8") as handle:
            ctx = json.load(handle)
    except (OSError, json.JSONDecodeError):
        ctx = None
    if isinstance(ctx, dict) and job_id in json.dumps(ctx, ensure_ascii=False):
        with open(ctx_path, "w", encoding="utf-8") as handle:
            json.dump({}, handle)
        print("line_pending_context: cleared")
    else:
        print("line_pending_context: not pointing to this job; kept")
print("STOP_DONE")
