#!/usr/bin/env python3
"""Fetch job after_qa from Railway into local fixture."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JOB = "job_20260614_003337_2026_0610_ヒロセ電機_海老様_森川様_福田_相原"
REMOTE = f"/app/data/transcriptions/{JOB}/merged_transcript_after_qa.txt"
OUT = ROOT / "scripts" / "fixtures" / "job_20260614_003337" / "merged_transcript_after_qa.txt"


def main() -> int:
    cmd = (
        ["railway.cmd", "ssh", f"cat {REMOTE}"]
        if os.name == "nt"
        else ["railway", "ssh", f"cat {REMOTE}"]
    )
    raw = subprocess.check_output(cmd)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(raw)
    text = raw.decode("utf-8")
    print(f"written={OUT} chars={len(text)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
