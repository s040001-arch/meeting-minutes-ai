#!/usr/bin/env python3
"""修正後テキストに残る「本社」の文脈を確認する。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from repair_thr_final_polish import apply_fixes  # noqa: E402

TEXT = Path("data/thr_after_qa_latest_u8.txt").read_text(encoding="utf-8")
fixed, _, _ = apply_fixes(TEXT)

out = []
idx = 0
while True:
    i = fixed.find("本社", idx)
    if i < 0:
        break
    s = max(0, i - 60)
    e = min(len(fixed), i + 65)
    out.append(f"@{i}: ...{fixed[s:e]}...".replace("\n", " "))
    idx = i + 1
Path("data/_thr_honsha_left.txt").write_text("\n\n".join(out), encoding="utf-8")
