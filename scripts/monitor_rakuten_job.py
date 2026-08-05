#!/usr/bin/env python3
"""楽天ジョブの状態を定期スナップショットし、変化があったときだけ出力する。"""
from __future__ import annotations

import subprocess
import sys
import time

CMD = [
    sys.executable,
    "scripts/_upload_run_thr.py",
    "scripts/watch_latest_job.py",
    "cd /app && PYTHONPATH=/app python3 scripts/watch_latest_job.py",
]

last = ""
for _poll in range(120):  # 最長6時間
    try:
        proc = subprocess.run(
            CMD, capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace",
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        # アップロードログ行を除いた本文のみ比較
        body = "\n".join(
            line
            for line in out.splitlines()
            if not line.strip().endswith("watch_latest_job.py")
        ).strip()
    except Exception as exc:  # noqa: BLE001
        body = f"MONITOR_ERROR={exc!r}"
    if body and body != last:
        stamp = time.strftime("%H:%M:%S")
        print(f"=== JOB_CHANGE {stamp} ===", flush=True)
        print(body, flush=True)
        last = body
        if "MONITOR_ERROR" not in body and "エラー" in body:
            print("=== JOB_ERROR_SEEN ===", flush=True)
    time.sleep(180)
