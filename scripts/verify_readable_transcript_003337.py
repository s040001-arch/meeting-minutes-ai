#!/usr/bin/env python3
"""Readable transcript BEFORE/AFTER gate for job_20260614_003337."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from readable_transcript import polish_transcript_text  # noqa: E402

JOB = "job_20260614_003337_2026_0610_ヒロセ電機_海老様_森川様_福田_相原"
REMOTE = f"/app/data/transcriptions/{JOB}"
LOCAL_AFTER_QA = ROOT / "scripts" / "fixtures" / "job_20260614_003337" / "merged_transcript_after_qa.txt"

CLOSING_NEEDLE = "最近あれこれ言いましたっけ"
GREETING_MARKERS = (
    "ありがとうございました",
    "お疲れ様",
    "ござい。",
    "いただきます",
)
CHECKLIST = [
    "85万",
    "75万",
    "6.5万円[要確認]",
    "五反田",
    "横浜",
]


def _fetch(name: str) -> bytes | None:
    cmd = (
        ["railway.cmd", "ssh", f"cat {REMOTE}/{name}"]
        if os.name == "nt"
        else ["railway", "ssh", f"cat {REMOTE}/{name}"]
    )
    try:
        return subprocess.check_output(cmd)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _closing_tail(text: str, *, tail_chars: int = 900) -> str:
    idx = text.find(CLOSING_NEEDLE)
    if idx >= 0:
        start = max(0, idx - tail_chars + len(CLOSING_NEEDLE))
        return text[start : idx + len(CLOSING_NEEDLE)]
    return text[-tail_chars:]


def _greeting_load(text: str) -> int:
    snippet = _closing_tail(text)
    return sum(snippet.count(marker) for marker in GREETING_MARKERS)


def _checklist(text: str) -> dict[str, bool]:
    return {term: term in text for term in CHECKLIST}


def main() -> int:
    if LOCAL_AFTER_QA.is_file():
        before = LOCAL_AFTER_QA.read_text(encoding="utf-8")
        print(f"source=local_fixture job={JOB}")
    else:
        raw = _fetch("merged_transcript_after_qa.txt")
        if raw is None:
            print("ERROR: could not fetch after_qa from Railway")
            return 1
        before = raw.decode("utf-8")
        print(f"source=railway job={JOB}")
    print(f"BEFORE_chars={len(before)}")

    after = polish_transcript_text(before)
    print(f"AFTER_chars={len(after)}")
    print(f"compression_ratio={len(after)/max(len(before),1):.3f}")

    print("\n=== CLOSING BEFORE ===")
    print(_closing_tail(before))
    print("\n=== CLOSING AFTER ===")
    print(_closing_tail(after))

    before_check = _checklist(before)
    after_check = _checklist(after)
    print("\n=== SUBSTANCE CHECKLIST ===")
    for term in CHECKLIST:
        print(
            f"{term}: before={before_check[term]} after={after_check[term]} "
            f"{'OK' if after_check[term] else 'MISSING'}"
        )

    missing = [t for t, ok in after_check.items() if not ok]
    before_greetings = _greeting_load(before)
    after_greetings = _greeting_load(after)
    closing_compressed = after_greetings < before_greetings
    print("\n=== GATE ===")
    print(f"closing_greetings_before={before_greetings} after={after_greetings}")
    print(f"closing_compressed={closing_compressed}")
    print(f"missing_terms={json.dumps(missing, ensure_ascii=False)}")
    print(f"headings_preserved={before.count('▼') <= after.count('▼') or before.count('▼') == 0}")

    with tempfile.TemporaryDirectory() as tmp:
        job_dir = os.path.join(tmp, JOB)
        os.makedirs(job_dir)
        after_path = os.path.join(job_dir, "merged_transcript_after_qa.txt")
        with open(after_path, "w", encoding="utf-8") as f:
            f.write(before)
        polish_transcript_text(before)
        with open(after_path, encoding="utf-8") as f:
            unchanged = f.read() == before
        print(f"after_qa_unchanged={unchanged}")

    if missing or not closing_compressed:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
