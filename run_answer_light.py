#!/usr/bin/env python3
"""QUESTION_MODE=cursor/line 用の軽量回答反映。

回答ごとに議事録再生成・Docs 全更新はしない。
  1. recorrect_from_line_answer（本文へ pinpoint / batch 反映）
  2. pending が残る → 次の質問を生成（必要なら LINE）し paused のまま
  3. pending が 0 → reprocess_job --from-step resume（6.1–6.3 を1回だけ）
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

from progress_tracker import update_job_progress
from question_mode import (
    clear_pause_marker,
    count_pending_unknowns,
    is_paused,
    should_send_line,
    write_pause_marker,
)
from repo_env import load_dotenv_local

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))


def _py() -> str:
    return sys.executable


def _run(cmd: list[str], *, log_path: str | None = None) -> int:
    print(f"[answer_light] run: {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, cwd=_REPO_ROOT)
    if log_path:
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"cmd={' '.join(cmd)} exit={result.returncode}\n")
        except OSError:
            pass
    return result.returncode


def _line_push_env_ready() -> bool:
    return bool(
        os.getenv("LINE_USER_ID", "").strip()
        and os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    )


def main() -> int:
    load_dotenv_local()
    parser = argparse.ArgumentParser(description="Light apply after LINE answer (QUESTION_MODE)")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--input-root", default="data/transcriptions")
    parser.add_argument(
        "--answers-json",
        default=os.path.join("data", "line_answers.json"),
    )
    parser.add_argument(
        "--send-line",
        action="store_true",
        help="pending が残る場合に次の質問を LINE 送信する",
    )
    parser.add_argument(
        "--min-question-value",
        type=int,
        default=7,
    )
    args = parser.parse_args()

    job_dir = os.path.join(args.input_root, args.job_id)
    log_path = os.path.join(job_dir, "answer_light_log.txt")
    os.makedirs(job_dir, exist_ok=True)

    update_job_progress(
        input_root=args.input_root,
        job_id=args.job_id,
        phase="answer_light_apply",
        status="running",
        detail={},
        overall_status="paused",
    )

    # 1) Apply answer(s) to transcript only
    rc = _run(
        [
            _py(),
            os.path.join(_REPO_ROOT, "recorrect_from_line_answer.py"),
            "--job-id",
            args.job_id,
            "--input-root",
            args.input_root,
            "--answers-json",
            args.answers_json,
        ],
        log_path=log_path,
    )
    if rc != 0:
        print(f"[answer_light] recorrect failed exit={rc}", flush=True)
        update_job_progress(
            input_root=args.input_root,
            job_id=args.job_id,
            phase="answer_light_apply",
            status="failed",
            detail={"exit": rc},
            overall_status="paused",
        )
        return rc

    pending = count_pending_unknowns(job_dir)
    print(f"[answer_light] pending_unknowns={pending}", flush=True)

    if pending > 0:
        # 2) Next question only (no minutes / docs)
        unknowns_path = os.path.join(job_dir, "unknown_points.json")
        from transcript_paths import resolve_transcript_path_for_minutes

        text_path = resolve_transcript_path_for_minutes(
            args.job_id, None, args.input_root
        )
        qcycle = [
            _py(),
            os.path.join(_REPO_ROOT, "run_question_cycle_once.py"),
            "--job-id",
            args.job_id,
            "--input-root",
            args.input_root,
            "--unknowns",
            unknowns_path,
            "--min-question-value",
            str(args.min_question_value),
        ]
        if text_path:
            qcycle.extend(["--text", text_path])
        send_line = bool(args.send_line or (should_send_line() and _line_push_env_ready()))
        if send_line:
            qcycle.append("--send-line")
        rc = _run(qcycle, log_path=log_path)
        if not is_paused(job_dir):
            write_pause_marker(
                job_dir,
                mode=os.environ.get("QUESTION_MODE", "line"),
                question_artifacts=["question_result.json", "unknown_points.json"],
                resume_hint=(
                    "回答を本文に反映したあと "
                    f"python reprocess_job.py --job-dir {job_dir} --from-step resume"
                ),
            )
        update_job_progress(
            input_root=args.input_root,
            job_id=args.job_id,
            phase="question_pause",
            status="success" if rc == 0 else "failed",
            detail={"pending_unknowns": pending, "next_question_exit": rc},
            overall_status="paused",
        )
        print(f"[answer_light] status=paused pending={pending}", flush=True)
        return 0 if rc == 0 else rc

    # 3) All answered → resume minutes once
    print("[answer_light] all answered; launching resume", flush=True)
    rc = _run(
        [
            _py(),
            os.path.join(_REPO_ROOT, "reprocess_job.py"),
            "--job-dir",
            job_dir,
            "--input-root",
            args.input_root,
            "--from-step",
            "resume",
            "--reason",
            "answer_light_all_answered",
        ],
        log_path=log_path,
    )
    if rc == 0:
        clear_pause_marker(job_dir)
        update_job_progress(
            input_root=args.input_root,
            job_id=args.job_id,
            phase="done",
            status="success",
            detail={"pending_unknowns": 0},
            overall_status="success",
        )
        print("[answer_light] status=success", flush=True)
    else:
        update_job_progress(
            input_root=args.input_root,
            job_id=args.job_id,
            phase="answer_light_resume",
            status="failed",
            detail={"exit": rc},
            overall_status="paused",
        )
        print(f"[answer_light] resume failed exit={rc}", flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
