#!/usr/bin/env python3
"""answered な unknown_points のフィールド構造を確認する。"""
from __future__ import annotations

import glob
import json

for pattern in ("job_20260804_140938*", "job_20260730_025256*"):
    dirs = glob.glob(f"/app/data/transcriptions/{pattern}")
    if not dirs:
        continue
    path = f"{dirs[0]}/unknown_points.json"
    try:
        data = json.load(open(path, encoding="utf-8"))
    except OSError:
        continue
    statuses: dict[str, int] = {}
    for item in data:
        key = str(item.get("status") or "")
        statuses[key] = statuses.get(key, 0) + 1
    print(pattern, "statuses:", statuses)
    done = [
        x
        for x in data
        if str(x.get("status") or "") not in ("open", "pending", "")
    ]
    for item in done[:2]:
        print(json.dumps(item, ensure_ascii=False)[:600])
    print("---")
