#!/usr/bin/env python3
"""ローカルコピーに polish + addendum を適用した現行相当テキストを作る。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from repair_thr_final_polish import apply_fixes  # noqa: E402

ADDENDUM = [
    ("私たちの魔法石だったとして、あの研修屋をなんだ。脱却したいみたいな",
     "私たちの方針としても、あの研修屋をなんとか脱却したいみたいな"),
    ("はいぜ、ちょっと前向きに検討、あのありがとうございます",
     "はい、ぜひちょっと前向きに検討、あのありがとうございます"),
]

TEXT = Path("data/thr_after_qa_latest_u8.txt").read_text(encoding="utf-8")
fixed, applied, missing = apply_fixes(TEXT)
for old, new in ADDENDUM:
    fixed = fixed.replace(old, new)
Path("data/thr_after_qa_fixed_local.txt").write_text(fixed, encoding="utf-8")
sys.stderr.write(f"applied={applied} missing={len(missing)} len={len(fixed)}\n")
