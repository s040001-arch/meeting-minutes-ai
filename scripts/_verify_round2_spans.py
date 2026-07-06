#!/usr/bin/env python3
"""round2 の span が現行相当テキストに全件一致するか確認する。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

text = Path("data/thr_after_qa_fixed_local.txt").read_text(encoding="utf-8")
items = json.loads(
    Path("scripts/fixtures/thr_span_hypothesis_round2.json").read_text(encoding="utf-8")
)
missing = 0
for it in items:
    span = it["span"]
    c = text.count(span)
    status = "OK" if c == 1 else ("MULTI" if c > 1 else "MISSING")
    if c != 1:
        missing += 1
    sys.stderr.write(f"[{status} x{c}] {span[:45]}\n")
sys.stderr.write(f"total={len(items)} problems={missing}\n")
sys.exit(1 if missing else 0)
