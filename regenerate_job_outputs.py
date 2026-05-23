"""既存ジョブの逐語録・議事録セクション・Google Docs を再生成する。

Railway 上でジョブデータが残っている場合、音声の再投入なしで品質改善を反映できる。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))


def _py() -> str:
    return sys.executable


def _run(cmd: list[str], step: str) -> None:
    print(f"[regenerate] {step}: {' '.join(cmd)}", flush=True)
    completed = subprocess.run(cmd, cwd=REPO_ROOT)
    if completed.returncode != 0:
        raise RuntimeError(f"{step} failed: exit_code={completed.returncode}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="既存ジョブの after_qa / 議事録セクション / Docs を再生成"
    )
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--input-root", default="data/transcriptions")
    parser.add_argument(
        "--answers-json",
        default=os.path.join("data", "line_answers.json"),
        help="指定時は LINE 回答を逐語録へ再反映してから後続処理",
    )
    parser.add_argument(
        "--skip-recorrect",
        action="store_true",
        help="LINE 回答の再反映をスキップ",
    )
    parser.add_argument(
        "--skip-diarize",
        action="store_true",
        help="話者交代の空行分割をスキップ",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="Google Docs を更新",
    )
    args = parser.parse_args()

    job_dir = os.path.join(args.input_root, args.job_id)
    if not os.path.isdir(job_dir):
        raise FileNotFoundError(f"job dir not found: {job_dir}")

    after_qa = os.path.join(job_dir, "merged_transcript_after_qa.txt")
    ai_path = os.path.join(job_dir, "merged_transcript_ai.txt")

    if not args.skip_recorrect and os.path.isfile(args.answers_json):
        _run(
            [
                _py(),
                os.path.join(REPO_ROOT, "recorrect_from_line_answer.py"),
                "--job-id",
                args.job_id,
                "--input-root",
                args.input_root,
                "--answers-json",
                args.answers_json,
                "--input",
                ai_path if os.path.isfile(ai_path) else after_qa,
                "--output",
                after_qa,
            ],
            "recorrect_from_line_answer",
        )

    if not args.skip_diarize:
        source = after_qa if os.path.isfile(after_qa) else ai_path
        if not os.path.isfile(source):
            raise FileNotFoundError(
                f"no transcript to diarize: {after_qa} or {ai_path}"
            )
        _run(
            [
                _py(),
                os.path.join(REPO_ROOT, "diarize_speakers.py"),
                "--job-id",
                args.job_id,
                "--input-root",
                args.input_root,
                "--input",
                source,
                "--output",
                after_qa if source == after_qa else ai_path,
            ],
            "diarize_speakers",
        )
        if source == ai_path and os.path.isfile(ai_path):
            import shutil

            shutil.copyfile(ai_path, after_qa)

    _run(
        [
            _py(),
            os.path.join(REPO_ROOT, "generate_minutes_transcript.py"),
            "--job-id",
            args.job_id,
            "--input-root",
            args.input_root,
        ],
        "generate_minutes_transcript",
    )
    _run(
        [
            _py(),
            os.path.join(REPO_ROOT, "generate_minutes_other_sections.py"),
            "--job-id",
            args.job_id,
            "--input-root",
            args.input_root,
        ],
        "generate_minutes_other_sections",
    )

    hub_cmd = [
        _py(),
        os.path.join(REPO_ROOT, "run_docs_hub_e2e.py"),
        "--job-id",
        args.job_id,
        "--input-root",
        args.input_root,
        "--skip-compose",
    ]
    if args.push:
        hub_cmd.append("--push")
    _run(hub_cmd, "run_docs_hub_e2e")

    print(f"regenerate_job_outputs_done job_id={args.job_id}")


if __name__ == "__main__":
    main()
