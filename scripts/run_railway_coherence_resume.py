#!/usr/bin/env python3
"""ローカルから Railway 上で restore + coherence 再実行（Windows の SSH クォート問題回避）。"""
from __future__ import annotations

import argparse
import subprocess
import sys


def _run(cmd: list[str]) -> int:
    print(" ".join(cmd), flush=True)
    completed = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace")
    return int(completed.returncode)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--service", default="meeting-minutes-ai")
    parser.add_argument("--job-id", required=True)
    parser.add_argument(
        "--subfolder-contains",
        required=True,
        help="Drive サブフォルダ名の部分一致",
    )
    parser.add_argument("--send-line", action="store_true")
    parser.add_argument("--push", action="store_true")
    args = parser.parse_args()

    restore_cmd = [
        "railway",
        "ssh",
        "-s",
        args.service,
        "--",
        "python3",
        "/app/restore_job_from_drive.py",
        "--job-id",
        args.job_id,
        "--subfolder-contains",
        args.subfolder_contains,
        "--rebuild-ai",
    ]
    code = _run(restore_cmd)
    if code != 0:
        return code

    resume_cmd = [
        "railway",
        "ssh",
        "-s",
        args.service,
        "--",
        "python3",
        "/app/run_resume_from_coherence.py",
        "--job-id",
        args.job_id,
    ]
    if args.send_line:
        resume_cmd.append("--send-line")
    if args.push:
        resume_cmd.append("--push")

    return _run(resume_cmd)


if __name__ == "__main__":
    raise SystemExit(main())
