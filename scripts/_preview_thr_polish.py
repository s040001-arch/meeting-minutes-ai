#!/usr/bin/env python3
"""ローカルコピーに FIXES を当てた結果のスポットプレビュー。"""
from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from repair_thr_final_polish import FIXES, apply_fixes  # noqa: E402

# repair_thr_final_polish が import 時に stdout を差し替えるため、その後で使う
_ = FIXES
sys.stdout = io.TextIOWrapper(open(1, "wb", closefd=False), encoding="utf-8", errors="replace")

TEXT = Path("data/thr_after_qa_latest_u8.txt").read_text(encoding="utf-8")
fixed, applied, missing = apply_fixes(TEXT)
print(f"applied={applied} missing={len(missing)}")

# 修正後テキストの主要箇所を表示
for anchor in [
    "NESTに関するあの", "実施要項とアンケート", "浅井が全部取りまとめて",
    "浅井みたいな専任", "ボックスに保管して", "同時編集でま常に最新",
    "課題感とか教えていただけます", "分かってはいるんですけどね",
    "実施要項で締結する部分", "そもそも実施要項ね", "認識の齟齬がない",
    "投影していただいたExcel", "本末転倒", "緊急連絡先", "オブザーバー",
    "NEST上に表示されてないものの方が少ない", "NEST上の実施要項をなくすことで",
    "オペレーションが変わってくる", "集合研修しかなく", "集まるだけがこう学び",
    "コーチングみたいな形", "品質高いあの研修", "検討が必要な段階",
    "教育期間", "研修数が増えることで", "探れますので", "常駐型",
    "Power BI", "別途私ができるかな", "目線合わせするポイント",
]:
    i = fixed.find(anchor)
    if i < 0:
        print(f"\n[MISS] {anchor}")
        continue
    s = max(0, i - 60)
    e = min(len(fixed), i + len(anchor) + 60)
    print(f"\n[{anchor}]\n  ...{fixed[s:e]}...".replace("\n\n", " "))

# 副作用チェック: 誤爆しやすい語が想定外に消えていないか
for w in ["本社", "弊社", "工数"]:
    print(f"\ncount[{w}]: before={TEXT.count(w)} after={fixed.count(w)}")

# 残存 本社 の文脈確認
idx = 0
while True:
    i = fixed.find("本社", idx)
    if i < 0:
        break
    s = max(0, i - 55)
    e = min(len(fixed), i + 60)
    print(f"\n[残本社] @{i}: ...{fixed[s:e]}...".replace("\n\n", " "))
    idx = i + 1
