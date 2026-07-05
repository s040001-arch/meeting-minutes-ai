#!/usr/bin/env python3
"""thrジョブ: delete 修復後に残った文脈破壊3箇所を、文が成立する形に修復して resume。

前回修復（literal除去のみ）の問題:
  1. 「あの等々でご相談」→ 助詞が宙吊り
  2. 「サポートお付き合い」→ 主語断片が孤立
  3. 「どのようにどのように」→ 置換時の重複
いずれも削除断片の最小範囲を広げて文として成立させる（内容の創作はしない）。
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from repo_env import load_dotenv_local  # noqa: E402

JOB_ID = "job_20260705_055804_2026_0624_thr社_運営改善_西脇様_竹中様_福田_相原"
INPUT_ROOT = "data/transcriptions"

CONTEXT_FIXES: list[tuple[str, str]] = [
    # 1) 宙吊りの「あの等々で」を含めて削除（「〜っていうところで、ご相談を…」に接続）
    (
        "っていうところで、あの等々でご相談をさせていただいてたので",
        "っていうところで、ご相談をさせていただいてたので",
    ),
    # 2) 孤立した「サポート」断片を削除（会話体の主語省略として自然に接続）
    (
        "今の御社のその形なんでしょう。\n\nサポートお付き合いさせていただいてます。",
        "今の御社のその形なんでしょう。\n\nお付き合いさせていただいてます。",
    ),
    # 3) 前回置換で生じた重複を解消
    (
        "誰から見ても、どのようにどのように",
        "誰から見ても、どのように",
    ),
]


def _py() -> str:
    return sys.executable


def _run(cmd: list[str], *, env: dict | None = None) -> int:
    print(f"run: {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, cwd=str(REPO), env=env or os.environ.copy()).returncode


def main() -> int:
    load_dotenv_local()
    job_dir = REPO / INPUT_ROOT / JOB_ID
    path = job_dir / "merged_transcript_after_qa.txt"
    if not path.is_file():
        print(f"missing={path}")
        return 1

    text = path.read_text(encoding="utf-8")
    n = 0
    for old, new in CONTEXT_FIXES:
        c = text.count(old)
        if c:
            text = text.replace(old, new)
            n += c
            print(f"fix x{c}: {old[:40]!r}... -> {new[:40]!r}...")
        else:
            print(f"not_found: {old[:50]!r}")
    if n == 0:
        print("no context fixes applied")
        return 1
    path.write_text(text, encoding="utf-8")
    print(f"after_qa_saved fixes={n}")

    env = os.environ.copy()
    env["QUESTION_MODE"] = env.get("QUESTION_MODE") or "line"
    rc = _run(
        [
            _py(),
            str(REPO / "reprocess_job.py"),
            "--job-dir",
            str(job_dir),
            "--input-root",
            INPUT_ROOT,
            "--from-step",
            "resume",
            "--reason",
            "thr_delete_context_repair",
        ],
        env=env,
    )
    if rc != 0:
        return rc

    from question_mode import clear_pause_marker
    from progress_tracker import update_job_progress
    from run_answer_light import _mark_doc_completed, send_completion_line

    clear_pause_marker(str(job_dir))
    _mark_doc_completed(str(job_dir), JOB_ID, INPUT_ROOT, "")
    send_line = bool(
        os.getenv("LINE_USER_ID", "").strip()
        and os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    )
    completion = send_completion_line(str(job_dir), job_id=JOB_ID, send_line=send_line)
    print(f"completion_line={completion}")
    update_job_progress(
        input_root=INPUT_ROOT,
        job_id=JOB_ID,
        phase="done",
        status="success",
        detail={"reason": "thr_delete_context_repair"},
        overall_status="success",
    )
    print("thr_delete_context_repair_done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
