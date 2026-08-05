#!/usr/bin/env python3
"""統合仕上げの結果と品質ゲート判定、注視トークンの行方を確認する。"""
from __future__ import annotations

import glob
import json
import os

job_dir = glob.glob("/app/data/transcriptions/job_20260805_012909*")[0]

with open(os.path.join(job_dir, "minutes_quality_gate.json"), encoding="utf-8") as f:
    gate = json.load(f)
print("=== quality gate ===")
print(json.dumps(gate, ensure_ascii=False)[:1200])

with open(os.path.join(job_dir, "unified_finishing_report.json"), encoding="utf-8") as f:
    rep = json.load(f)
stats = rep.get("stats") or {}
print("=== unified stats ===")
print(json.dumps(stats, ensure_ascii=False)[:800])
report = rep.get("report") or {}
findings = report.get("findings") or []
print(f"remaining_findings={len(findings)}")
for x in findings[:10]:
    print(
        "-",
        str(x.get("confidence"))[:6],
        str(x.get("quote") or "")[:50],
        "|",
        str(x.get("issue") or "")[:40],
    )

with open(os.path.join(job_dir, "merged_transcript_readable.txt"), encoding="utf-8") as f:
    readable = f.read()
for token in ("山谷", "山屋", "シュニア", "シニア", "山家"):
    print(f"readable count {token}: {readable.count(token)}")

with open(os.path.join(job_dir, "progress.json"), encoding="utf-8") as f:
    progress = json.load(f)
print("phase:", progress.get("phase"), "overall:", progress.get("overall_status"))
for ev in (progress.get("events") or [])[-4:]:
    print(json.dumps(ev, ensure_ascii=False)[:160])
