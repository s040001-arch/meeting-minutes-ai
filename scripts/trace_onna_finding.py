#!/usr/bin/env python3
"""「少し女使って」がTHR統合処理でどう扱われたかをログから追跡する。"""
from __future__ import annotations

import glob
import json
import os

job_dir = glob.glob("/app/data/transcriptions/job_20260730_025256*")[0]
report_path = os.path.join(job_dir, "unified_finishing_report.json")
with open(report_path, encoding="utf-8") as handle:
    data = json.load(handle)
report = data["report"]
needle = "女使って"

for round_info in report.get("rounds", []):
    stage = round_info.get("stage")
    for kind in ("findings", "applied", "skipped"):
        for item in round_info.get(kind) or []:
            blob = json.dumps(item, ensure_ascii=False)
            if needle in blob:
                print(f"[{stage}] {kind}:")
                print(
                    json.dumps(
                        {
                            "quote": str(item.get("quote") or "")[:80],
                            "issue": str(item.get("issue") or "")[:80],
                            "fix": str(item.get("fix") or "")[:80],
                            "confidence": item.get("confidence"),
                            "skip_reason": item.get("skip_reason")
                            or item.get("reason"),
                        },
                        ensure_ascii=False,
                    )
                )

# 全skipped理由の集計（ゲートで死んだ修正の全体像）
from collections import Counter

reasons: Counter[str] = Counter()
for round_info in report.get("rounds", []):
    for item in round_info.get("skipped") or []:
        reason = str(item.get("skip_reason") or item.get("reason") or "?")
        reasons[str(reason)[:40]] += 1
print("=== skipped reasons ===")
for reason, count in reasons.most_common(12):
    print(f"{count:3d}  {reason}")
print(
    "totals: findings_r1={} applied={} skipped={}".format(
        len((report.get("rounds") or [{}])[0].get("findings") or []),
        len(report.get("applied") or []),
        len(report.get("skipped") or []),
    )
)
