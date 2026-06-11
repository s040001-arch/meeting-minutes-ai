#!/usr/bin/env python3
"""Railway コンテナ内で実行する restore + coherence 再開（UTF-8 job_id を安全に保持）。"""
from __future__ import annotations

import argparse
import subprocess
import sys

# Windows SSH 経由では日本語 job_id が化けるため、ここで UTF-8 定義を保持する。
KNOWN_JOBS: dict[str, tuple[str, str]] = {
    "20260611_030404": (
        "job_20260611_030404_2026_0610_ヒロセ電機_海老様_森川様_福田_相原",
        "2026_0610_ヒロセ",
    ),
}


def _run(cmd: list[str]) -> None:
    print(" ".join(cmd), flush=True)
    completed = subprocess.run(cmd)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--key",
        default="20260611_030404",
        help="KNOWN_JOBS のキー（ASCII のみ）",
    )
    parser.add_argument("--send-line", action="store_true")
    parser.add_argument("--push", action="store_true")
    parser.add_argument(
        "--skip-restore",
        action="store_true",
        help="ジョブ復元済みの場合 coherence 以降のみ実行",
    )
    args = parser.parse_args()

    entry = KNOWN_JOBS.get(args.key)
    if not entry:
        raise SystemExit(f"unknown job key: {args.key}")
    job_id, subfolder_contains = entry
    py = sys.executable

    if not args.skip_restore:
        _run(
            [
                py,
                "/app/restore_job_from_drive.py",
                "--job-id",
                job_id,
                "--subfolder-contains",
                subfolder_contains,
                "--rebuild-ai",
            ]
        )

    resume_cmd = [
        py,
        "/app/run_resume_from_coherence.py",
        "--job-id",
        job_id,
    ]
    if args.send_line:
        resume_cmd.append("--send-line")
    if args.push:
        resume_cmd.append("--push")
    _run(resume_cmd)
    print(f"railway_remote_resume_done key={args.key} job_id={job_id}")


if __name__ == "__main__":
    main()
