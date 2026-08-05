#!/usr/bin/env python3
"""最新ジョブの状態スナップショットを1回出力する（ウォッチ用）。"""
from __future__ import annotations

import glob
import json
import os

roots = sorted(
    glob.glob("/app/data/transcriptions/job_*"),
    key=os.path.getmtime,
    reverse=True,
)
if not roots:
    print("NO_JOBS")
    raise SystemExit(0)
job_dir = roots[0]
print(f"job_dir={os.path.basename(job_dir)}")

status_path = os.path.join(job_dir, "status.json")
if os.path.isfile(status_path):
    with open(status_path, encoding="utf-8") as handle:
        status = json.load(handle)
    print(
        "status:",
        json.dumps(
            {
                k: status.get(k)
                for k in (
                    "overall_status",
                    "current_step",
                    "updated_at",
                    "error",
                )
                if k in status
            },
            ensure_ascii=False,
        ),
    )
else:
    print("status: (no status.json)")

for name in sorted(os.listdir(job_dir)):
    path = os.path.join(job_dir, name)
    if os.path.isfile(path):
        print(f"file: {name} size={os.path.getsize(path)}")

unknown_path = os.path.join(job_dir, "unknown_points.json")
if os.path.isfile(unknown_path):
    with open(unknown_path, encoding="utf-8") as handle:
        points = json.load(handle)
    counts: dict[str, int] = {}
    for p in points:
        st = str(p.get("status") or "?")
        counts[st] = counts.get(st, 0) + 1
    print(f"unknown_points: {counts}")

report_path = os.path.join(job_dir, "unified_finishing_report.json")
if os.path.isfile(report_path):
    with open(report_path, encoding="utf-8") as handle:
        data = json.load(handle)
    stats = data.get("stats") or {}
    print(
        "unified:",
        json.dumps(
            {
                k: stats.get(k)
                for k in (
                    "windows",
                    "audit_findings",
                    "auto_applied",
                    "resolver_applied",
                    "dense_repair_applied",
                    "remaining_findings",
                    "duration_sec",
                    "answered_knowledge_items",
                )
                if k in stats
            },
            ensure_ascii=False,
        ),
    )
