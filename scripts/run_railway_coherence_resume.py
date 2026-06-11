#!/usr/bin/env python3
"""ローカルから Railway 上の railway_remote_resume_job.py を起動する。"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys


def _railway_cmd() -> list[str]:
    if sys.platform == "win32":
        path = shutil.which("railway")
        if path and path.lower().endswith(".ps1"):
            return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path]
        if path:
            return [path]
    exe = shutil.which("railway")
    if not exe:
        raise SystemExit("railway CLI not found in PATH")
    return [exe]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--service", default="meeting-minutes-ai")
    parser.add_argument("--key", default="20260611_030404")
    parser.add_argument("--send-line", action="store_true")
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--skip-restore", action="store_true")
    args = parser.parse_args()

    cmd = [
        *_railway_cmd(),
        "ssh",
        "-s",
        args.service,
        "--",
        "python3",
        "/app/scripts/railway_remote_resume_job.py",
        "--key",
        args.key,
    ]
    if args.send_line:
        cmd.append("--send-line")
    if args.push:
        cmd.append("--push")
    if args.skip_restore:
        cmd.append("--skip-restore")

    print(" ".join(cmd), flush=True)
    completed = subprocess.run(cmd)
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
