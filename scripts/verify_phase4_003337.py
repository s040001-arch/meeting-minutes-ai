#!/usr/bin/env python3
"""Phase 4 local verification against job_20260614_003337 (Railway or synthetic)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from line_answer_reflect import load_after_qa_text  # noqa: E402
from tests.test_line_answer_reflect_phase4 import (  # noqa: E402
    _answers_sequence,
    _synthetic_003337_transcript,
)

JOB = "job_20260614_003337_2026_0610_ヒロセ電機_海老様_森川様_福田_相原"
REMOTE = f"/app/data/transcriptions/{JOB}"


def _fetch(name: str) -> bytes | None:
    cmd = ["railway.cmd", "ssh", f"cat {REMOTE}/{name}"] if os.name == "nt" else ["railway", "ssh", f"cat {REMOTE}/{name}"]
    try:
        return subprocess.check_output(cmd)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _count_targets(text: str) -> dict[str, int]:
    keys = [
        "7.5万円[要確認]",
        "6.5万円[要確認]",
        "10倍ぐらい[要確認]",
        "熱費ができない[要確認]",
        "75万円",
        "65万円",
        "[要確認]",
    ]
    return {k: text.count(k) for k in keys}


def main() -> int:
    ai_raw = _fetch("merged_transcript_ai.txt")
    use_synthetic = ai_raw is None
    if use_synthetic:
        print("railway fetch unavailable; using synthetic 003337 excerpt")
        before = _synthetic_003337_transcript()
    else:
        before = ai_raw.decode("utf-8")

    before_counts = _count_targets(before)
    print("BEFORE", json.dumps(before_counts, ensure_ascii=False))

    with tempfile.TemporaryDirectory() as tmp:
        job_dir = os.path.join(tmp, JOB)
        os.makedirs(job_dir)
        ai_path = os.path.join(job_dir, "merged_transcript_ai.txt")
        with open(ai_path, "w", encoding="utf-8") as f:
            f.write(before)

        from unittest.mock import patch

        from recorrect_from_line_answer import _handle_coherence_single_answer

        for step in _answers_sequence():
            su = step["unknown"]
            qresult = {"selected_unknown": su, "question_text": step["question_text"]}
            with patch(
                "recorrect_from_line_answer._persist_coherence_answer_to_learned_dict",
                return_value={"action": "noop"},
            ):
                _handle_coherence_single_answer(
                    job_id=JOB,
                    input_root=tmp,
                    question_result=qresult,
                    answer_text=step["answer_text"],
                    question_id=step["question_id"],
                    out_path=os.path.join(job_dir, "merged_transcript_after_qa.txt"),
                )

        after = load_after_qa_text(job_dir)
        after_counts = _count_targets(after)
        print("AFTER", json.dumps(after_counts, ensure_ascii=False))
        print("source", "synthetic" if use_synthetic else "railway")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
