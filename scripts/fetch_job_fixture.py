#!/usr/bin/env python3
"""Download job fixtures from Railway for local replay."""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOB = "/app/data/transcriptions/job_20260612_223435_2026_0610_ヒロセ電機_海老様_森川様_福田_相原"
OUT = os.path.join(ROOT, "scripts", "fixtures", "job_20260612_223435")
FILES = (
    ("transcript_anomalies.json", "transcript_anomalies.before.json"),
    ("merged_transcript_ai.txt", "merged_transcript_ai.txt"),
    ("meeting_profile.json", "meeting_profile.json"),
)


def _fetch(name: str) -> bytes:
    remote = f"cat {JOB}/{name}"
    cmd = ["railway.cmd", "ssh", remote] if os.name == "nt" else ["railway", "ssh", remote]
    return subprocess.check_output(cmd)


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    for remote_name, local_name in FILES:
        path = os.path.join(OUT, local_name)
        data = _fetch(remote_name)
        with open(path, "wb") as f:
            f.write(data)
        print(f"wrote {path} ({len(data)} bytes)")
    with open(os.path.join(OUT, "transcript_anomalies.before.json"), encoding="utf-8") as f:
        payload = json.load(f)
    print("anomalies_count", len(payload.get("anomalies", [])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
