#!/usr/bin/env python3
"""検証環境(/tmp)に残る昨夜のTHRレポートからゲート却下の実態を確認する。"""
from __future__ import annotations

import json
from collections import Counter

path = "/tmp/unified_e2e/job_20260730_025256/final_review_report.json"
with open(path, encoding="utf-8") as handle:
    report = json.load(handle)

needle = "女使って"
for round_info in report.get("rounds", []):
    stage = round_info.get("stage")
    for kind in ("findings", "applied", "skipped"):
        for item in round_info.get(kind) or []:
            if needle in json.dumps(item, ensure_ascii=False):
                print(
                    f"[{stage}] {kind}:",
                    json.dumps(
                        {
                            "quote": str(item.get("quote") or "")[:60],
                            "fix": str(item.get("fix") or "")[:60],
                            "confidence": item.get("confidence"),
                            "reason": item.get("skip_reason")
                            or item.get("reason"),
                        },
                        ensure_ascii=False,
                    ),
                )

reasons: Counter[str] = Counter()
for round_info in report.get("rounds", []):
    for item in round_info.get("skipped") or []:
        reason = str(item.get("skip_reason") or item.get("reason") or "?")
        reasons[reason[:44]] += 1
print("=== skipped reasons (Opus4.8 run) ===")
for reason, count in reasons.most_common(15):
    print(f"{count:3d}  {reason}")
