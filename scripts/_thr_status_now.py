#!/usr/bin/env python3
"""thrジョブ: progress / pause / 直近回答 / span_hypothesis unknown状態を出力。"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

JOB_ID = "job_20260705_055804_2026_0624_thr社_運営改善_西脇様_竹中様_福田_相原"
JOB = Path("data/transcriptions") / JOB_ID


def main() -> int:
    prog = json.loads((JOB / "progress.json").read_text(encoding="utf-8"))
    print(f"overall={prog.get('overall_status')} phase={prog.get('phase')}")
    print(f"pause_marker={(JOB / 'question_pause.json').is_file()}")

    la = Path("data/line_answers.json")
    if la.is_file():
        recs = json.loads(la.read_text(encoding="utf-8"))
        ours = [r for r in recs if r.get("job_id") == JOB_ID]
        for r in ours[-3:]:
            print(f"(line) t={r.get('created_at','')} answer={str(r.get('answer_text'))[:200]!r}")
    ans = JOB / "answers.json"
    if ans.is_file():
        recs = json.loads(ans.read_text(encoding="utf-8"))
        for r in recs[-3:]:
            print(f"(job) qid={str(r.get('question_id'))[:8]} answer={str(r.get('answer_text'))[:200]!r}")

    ups = json.loads((JOB / "unknown_points.json").read_text(encoding="utf-8"))
    n_open = sum(1 for u in ups if u.get("status") == "open")
    n_asked = sum(1 for u in ups if u.get("status") == "asked")
    print(f"unknowns: total={len(ups)} open={n_open} asked={n_asked}")
    for u in ups:
        if u.get("question_kind") == "span_hypothesis" or u.get("reentry_source") == "manual_reentry":
            print(
                f"  [{u.get('status')}] kind={u.get('question_kind','-')} "
                f"word={str(u.get('anomaly_word'))[:40]!r} "
                f"action={u.get('correction_action','-')}"
            )
    qr = JOB / "question_result.json"
    if qr.is_file():
        d = json.loads(qr.read_text(encoding="utf-8"))
        print(f"question_result: status={d.get('question_status')} qid={str(d.get('question_id'))[:8]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
