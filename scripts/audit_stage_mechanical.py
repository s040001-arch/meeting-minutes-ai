#!/usr/bin/env python3
"""工程監査1: 機械補正(辞書置換)が何をどう変えたかを確認する。

チェック観点:
- 置換された語と回数（学習辞書由来の置換が妥当か）
- 過剰置換の兆候（同一語の大量置換）
- 補正前後の文字数差が異常でないか
"""
from __future__ import annotations

import difflib
import glob
import os

roots = sorted(
    glob.glob("/app/data/transcriptions/job_20260805_012909*"),
    key=os.path.getmtime,
)
job_dir = roots[0]

with open(
    os.path.join(job_dir, "merged_transcript.txt"), encoding="utf-8"
) as handle:
    before = handle.read()
with open(
    os.path.join(job_dir, "merged_transcript_mechanical.txt"),
    encoding="utf-8",
) as handle:
    after = handle.read()

print(f"chars_before={len(before)} chars_after={len(after)}")

matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
changes = 0
for tag, i1, i2, j1, j2 in matcher.get_opcodes():
    if tag == "equal":
        continue
    changes += 1
    if changes > 60:
        continue
    ctx_pre = before[max(0, i1 - 20) : i1].replace("\n", " ")
    ctx_post = before[i2 : i2 + 20].replace("\n", " ")
    old = before[i1:i2].replace("\n", "\\n")
    new = after[j1:j2].replace("\n", "\\n")
    print(f"[{tag}] …{ctx_pre}『{old}』→『{new}』{ctx_post}…")
print(f"total_change_regions={changes}")

# 学習辞書の中身（このジョブに効きうる項目）
try:
    import json

    with open("/app/data/learned_corrections.json", encoding="utf-8") as f:
        learned = json.load(f)
    print(f"learned_dict_entries={len(learned)}")
    for entry in learned[-15:]:
        print(
            "learned:",
            json.dumps(entry, ensure_ascii=False)[:160],
        )
except Exception as exc:  # noqa: BLE001
    print(f"learned_dict_read_failed={exc!r}")
