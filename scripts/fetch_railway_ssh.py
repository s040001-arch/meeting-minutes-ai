#!/usr/bin/env python3
"""Fetch job artifacts from Railway via `railway ssh` + base64."""

from __future__ import annotations

import argparse
import base64
import subprocess
import sys
from pathlib import Path


def fetch_file(service: str, remote_path: str) -> bytes:
    cmd = [
        "railway",
        "ssh",
        "-s",
        service,
        "--",
        "base64",
        remote_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, shell=sys.platform == "win32")
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"railway ssh failed for {remote_path}: {err}")
    raw = proc.stdout.decode("ascii", errors="replace").strip()
    if not raw:
        raise RuntimeError(f"empty payload for {remote_path}")
    return base64.b64decode(raw)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--service", default="meeting-minutes-ai")
    parser.add_argument(
        "--out-dir",
        default="data/transcriptions/_railway_fetch",
    )
    parser.add_argument(
        "--files",
        nargs="*",
        default=[
            "merged_transcript.txt",
            "merged_transcript_ai.txt",
            "merged_transcript_after_qa.txt",
            "merged_transcript_mechanical.txt",
            "correction_meta.json",
            "minutes_draft.md",
            "minutes_structured.md",
            "minutes_sections_raw.json",
            "e2e_run_log.txt",
            "meeting_profile.json",
            "answers.json",
            "question_result.json",
            "docs_write_log.txt",
        ],
    )
    args = parser.parse_args()
    out_dir = Path(args.out_dir) / args.job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    remote_dir = f"/app/data/transcriptions/{args.job_id}"

    for name in args.files:
        remote = f"{remote_dir}/{name}"
        local = out_dir / name
        try:
            data = fetch_file(args.service, remote)
        except RuntimeError as e:
            print(f"[skip] {name}: {e}", file=sys.stderr)
            continue
        local.write_bytes(data)
        print(f"{name}: {len(data)} bytes -> {local}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
