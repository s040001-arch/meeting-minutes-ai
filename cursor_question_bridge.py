#!/usr/bin/env python3
"""Channel-neutral bridge for answering paused questions from Cursor chat.

The production pipeline keeps generating the same question_result.json and
unknown_points.json artifacts.  This CLI lets an agent inspect and answer them
without LINE, while the existing LINE webhook remains available when
QUESTION_MODE=line.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from question_mode import is_paused


DEFAULT_INPUT_ROOT = "data/transcriptions"
ANSWER_LOG = "cursor_answer_light.log"


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _question_payload(job_dir: Path) -> dict[str, Any] | None:
    payload = _load_json(job_dir / "question_result.json")
    if not isinstance(payload, dict):
        return None
    if str(payload.get("question_status") or "") != "generated":
        return None
    if not str(payload.get("question_id") or "").strip():
        return None
    return payload


def pending_jobs(input_root: str | Path) -> list[tuple[Path, dict[str, Any]]]:
    root = Path(input_root)
    found: list[tuple[Path, dict[str, Any]]] = []
    if not root.is_dir():
        return found
    for job_dir in root.glob("job_*"):
        if not job_dir.is_dir() or not is_paused(job_dir):
            continue
        payload = _question_payload(job_dir)
        if payload is not None:
            found.append((job_dir, payload))
    found.sort(
        key=lambda pair: (
            pair[0] / "question_result.json"
        ).stat().st_mtime,
        reverse=True,
    )
    return found


def resolve_job(
    input_root: str | Path,
    job_id: str | None,
) -> tuple[Path, dict[str, Any]]:
    root = Path(input_root)
    if job_id:
        job_dir = root / job_id
        payload = _question_payload(job_dir)
        if payload is None or not is_paused(job_dir):
            raise RuntimeError(
                f"job has no paused generated question: {job_id}"
            )
        return job_dir, payload
    found = pending_jobs(root)
    if not found:
        raise RuntimeError("no paused Cursor questions")
    if len(found) > 1:
        ids = ", ".join(job.name for job, _ in found)
        raise RuntimeError(
            "multiple paused Cursor questions; specify --job-id: " + ids
        )
    return found[0]


def public_question(
    job_dir: Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "job_id": job_dir.name,
        "question_id": str(payload.get("question_id") or ""),
        "question_format": str(payload.get("question_format") or ""),
        "question_text": str(payload.get("question_text") or "").strip(),
        "doc_url": str(payload.get("doc_url") or "").strip(),
        "source_transcript_url": str(
            payload.get("source_transcript_url") or ""
        ).strip(),
    }


def append_job_answer(
    job_dir: Path,
    payload: dict[str, Any],
    answer_text: str,
) -> tuple[Path, bool]:
    answer = str(answer_text or "").strip()
    if not answer:
        raise ValueError("answer is empty")
    path = job_dir / "answers.json"
    existing = _load_json(path)
    rows = existing if isinstance(existing, list) else []
    question_id = str(payload.get("question_id") or "").strip()
    for row in rows:
        if (
            isinstance(row, dict)
            and str(row.get("question_id") or "") == question_id
        ):
            return path, False
    rows.append(
        {
            "received_at": datetime.now(timezone.utc).isoformat(),
            "question_id": question_id,
            "question_text": str(payload.get("question_text") or ""),
            "answer_text": answer,
            "user_id": "cursor-chat",
            "job_id": job_dir.name,
            "channel": "cursor",
        }
    )
    path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path, True


def launch_answer_resume(
    *,
    job_dir: Path,
    input_root: str | Path,
    answers_path: Path,
) -> int:
    repo = Path(__file__).resolve().parent
    log_path = job_dir / ANSWER_LOG
    env = dict(os.environ)
    env["QUESTION_MODE"] = "cursor"
    with log_path.open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                str(repo / "run_answer_light.py"),
                "--job-id",
                job_dir.name,
                "--input-root",
                str(input_root),
                "--answers-json",
                str(answers_path),
            ],
            cwd=repo,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    submission = {
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "job_id": job_dir.name,
        "pid": process.pid,
        "answers_path": str(answers_path),
        "log_path": str(log_path),
    }
    (job_dir / "cursor_answer_submission.json").write_text(
        json.dumps(submission, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return process.pid


def _answer_from_args(args: argparse.Namespace) -> str:
    if args.answer_base64:
        return base64.b64decode(args.answer_base64).decode("utf-8")
    return str(args.answer or "")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", default=DEFAULT_INPUT_ROOT)
    sub = parser.add_subparsers(dest="command", required=True)

    pending = sub.add_parser("pending")
    pending.add_argument("--job-id", default=None)

    answer = sub.add_parser("answer")
    answer.add_argument("--job-id", default=None)
    answer_group = answer.add_mutually_exclusive_group(required=True)
    answer_group.add_argument("--answer")
    answer_group.add_argument("--answer-base64")
    args = parser.parse_args()

    if args.command == "pending":
        if args.job_id:
            job_dir, payload = resolve_job(
                args.input_root, args.job_id
            )
            result = [public_question(job_dir, payload)]
        else:
            result = [
                public_question(job_dir, payload)
                for job_dir, payload in pending_jobs(args.input_root)
            ]
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    job_dir, payload = resolve_job(args.input_root, args.job_id)
    answers_path, appended = append_job_answer(
        job_dir, payload, _answer_from_args(args)
    )
    pid = None
    if appended:
        pid = launch_answer_resume(
            job_dir=job_dir,
            input_root=args.input_root,
            answers_path=answers_path,
        )
    print(
        json.dumps(
            {
                "job_id": job_dir.name,
                "question_id": payload.get("question_id"),
                "answer_appended": appended,
                "resume_pid": pid,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
