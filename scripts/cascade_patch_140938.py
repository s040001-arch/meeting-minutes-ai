#!/usr/bin/env python3
"""楽天ジョブ: 回答済みの事実(俊也・山屋)を本文へ横展開し、同種の質問を消化する。

scan: 現状の残存箇所と対象の未質問アイテムを表示するだけ。
apply: ジョブが paused のときだけ、置換とアイテム解決を実施する。
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from datetime import datetime

MODE = sys.argv[1] if len(sys.argv) > 1 else "scan"
job_dir = glob.glob("/app/data/transcriptions/job_20260804_140938*")[0]
AFTER_QA = os.path.join(job_dir, "merged_transcript_after_qa.txt")

with open(AFTER_QA, encoding="utf-8") as handle:
    text = handle.read()

# 確定済みの事実（ユーザーがLINEで回答済み）
# 1) AIオフィサー文脈の「シニア/シュニア」は「俊也」
# 2) 「山谷さん」は参加者「山屋さん」
name_re = re.compile(r"山谷(さん|様)")
officer_token_re = re.compile(r"シュニア|シニア")


def officer_spans():
    spans = []
    for match in officer_token_re.finditer(text):
        start = max(0, match.start() - 60)
        end = min(len(text), match.end() + 60)
        if "AIオフィサー" in text[start:end]:
            spans.append((match.start(), match.end(), match.group(0)))
    return spans


print("=== scan ===")
print("yamatani_occurrences:", len(name_re.findall(text)))
spans = officer_spans()
print("officer_senior_occurrences:", len(spans))
for start, end, token in spans:
    print("  ctx:", text[max(0, start - 40) : end + 40].replace("\n", " "))

unknown_path = os.path.join(job_dir, "unknown_points.json")
with open(unknown_path, encoding="utf-8") as handle:
    unknowns = json.load(handle)

TERMINAL = {"answered", "resolved", "done", "closed"}
targets = []
for item in unknowns:
    status = str(item.get("status") or "").strip().lower()
    if status in TERMINAL or status == "asked":
        continue
    surface = " ".join(
        str(item.get(k) or "") for k in ("text", "anomaly_word", "evidence")
    )
    if "山谷" in surface or (
        ("シニア" in surface or "シュニア" in surface)
        and "AIオフィサー" in surface
    ):
        targets.append(item)
print("pending_sibling_items:", len(targets))
for item in targets:
    print("  -", str(item.get("text") or "")[:70])

if MODE != "apply":
    sys.exit(0)

progress = json.load(open("/app/data/last_job_progress.json", encoding="utf-8"))
if progress.get("overall_status") != "paused":
    print("job is not paused; aborting apply")
    sys.exit(1)

new_text = name_re.sub(lambda m: "山屋" + m.group(1), text)
# officer spans を置換後テキストで再計算してから後ろから置換する
out_text = new_text
spans2 = []
for match in officer_token_re.finditer(out_text):
    s = max(0, match.start() - 60)
    e = min(len(out_text), match.end() + 60)
    if "AIオフィサー" in out_text[s:e]:
        spans2.append((match.start(), match.end()))
for s, e in reversed(spans2):
    out_text = out_text[:s] + "俊也" + out_text[e:]

changed = out_text != text
if changed:
    with open(AFTER_QA, "w", encoding="utf-8") as handle:
        handle.write(out_text)

now = datetime.now().isoformat(timespec="seconds")
audit_rows = [
    {
        "at": now,
        "wrong": "山谷",
        "correct": "山屋",
        "applied_count": len(name_re.findall(text)),
        "status": "applied_cascade_manual",
    },
    {
        "at": now,
        "wrong": "シニア/シュニア(AIオフィサー文脈)",
        "correct": "俊也",
        "applied_count": len(spans2),
        "status": "applied_cascade_manual",
    },
]
with open(
    os.path.join(job_dir, "line_correction_audit.jsonl"), "a", encoding="utf-8"
) as handle:
    for row in audit_rows:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")

resolved = 0
for item in targets:
    item["status"] = "resolved"
    item["resolved_via"] = "cascade_manual_from_answered_facts"
    item["answer"] = "確定済み回答の横展開(俊也/山屋)"
    resolved += 1
if resolved:
    with open(unknown_path, "w", encoding="utf-8") as handle:
        json.dump(unknowns, handle, ensure_ascii=False, indent=2)

print(
    f"applied changed={changed} yamatani={audit_rows[0]['applied_count']} "
    f"officer={len(spans2)} resolved_items={resolved}"
)
