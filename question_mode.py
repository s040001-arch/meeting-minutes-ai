"""QUESTION_MODE: pause pipeline after question generation until answers are applied.

Modes (env QUESTION_MODE, default off):
  off / empty / false / 0  — legacy path: questions then minutes in one run
  on  / cursor             — generate question MD, pause (exit 0); no LINE push
  line                     — generate questions, push LINE if credentials ready, pause

Resume: reprocess_job.py --from-step resume (runs Step 6.1–6.3 after body is fixed).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PAUSE_FILENAME = "question_pause.json"
QUESTIONS_REVIEW_MD = "questions_review.md"

_OFF_VALUES = frozenset({"", "off", "false", "0", "no", "disabled"})
_CURSOR_VALUES = frozenset({"on", "cursor", "md", "true", "1", "yes"})
_LINE_VALUES = frozenset({"line", "push"})


def resolve_question_mode(raw: str | None = None) -> str:
    """Return 'off' | 'cursor' | 'line'."""
    if raw is None:
        raw = os.environ.get("QUESTION_MODE", "")
    v = str(raw or "").strip().lower()
    if v in _OFF_VALUES:
        return "off"
    if v in _LINE_VALUES:
        return "line"
    if v in _CURSOR_VALUES:
        return "cursor"
    # Unknown values fail closed to off (no surprise pause in production).
    return "off"


def should_pause_for_answers(mode: str | None = None) -> bool:
    return resolve_question_mode(mode) != "off"


def should_send_line(mode: str | None = None) -> bool:
    """LINE push only when mode=line (cursor/on never auto-pushes)."""
    return resolve_question_mode(mode) == "line"


def pause_path(job_dir: str | Path) -> Path:
    return Path(job_dir) / PAUSE_FILENAME


def write_pause_marker(
    job_dir: str | Path,
    *,
    mode: str,
    question_artifacts: list[str],
    resume_hint: str,
) -> Path:
    job = Path(job_dir)
    payload: dict[str, Any] = {
        "status": "paused_waiting_answers",
        "mode": resolve_question_mode(mode),
        "paused_at": datetime.now(timezone.utc).isoformat(),
        "question_artifacts": question_artifacts,
        "resume_hint": resume_hint,
        "resume_command": (
            f"python reprocess_job.py --job-dir {job} --from-step resume"
        ),
    }
    path = pause_path(job)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def clear_pause_marker(job_dir: str | Path) -> bool:
    path = pause_path(job_dir)
    if not path.is_file():
        return False
    path.unlink()
    return True


def is_paused(job_dir: str | Path) -> bool:
    """True only while waiting for answers.

    Terminal overall_status (success/failed/…) wins over a stale pause marker so
    monitors never treat a finished job as still awaiting answers.
    """
    job = Path(job_dir)
    progress = job / "progress.json"
    if progress.is_file():
        try:
            payload = json.loads(progress.read_text(encoding="utf-8"))
            overall = str(payload.get("overall_status") or "").strip().lower()
            if overall in {"success", "failed", "error", "done"}:
                return False
        except (OSError, json.JSONDecodeError):
            pass
    path = pause_path(job)
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    return str(data.get("status") or "") == "paused_waiting_answers"


_TERMINAL_OVERALL = frozenset({"success", "failed", "error", "done"})


def clear_pause_on_terminal(job_dir: str | Path, overall_status: str | None) -> bool:
    """Drop pause marker when the job reaches a terminal overall_status."""
    status = str(overall_status or "").strip().lower()
    if status not in _TERMINAL_OVERALL:
        return False
    return clear_pause_marker(job_dir)


def _is_answered_unknown(item: dict) -> bool:
    status = str(item.get("status", "")).strip().lower()
    if status in {"answered", "done", "closed", "resolved"}:
        return True
    answer = item.get("answer")
    return isinstance(answer, str) and bool(answer.strip())


def load_unknown_points(job_dir: str | Path) -> list[dict[str, Any]]:
    path = Path(job_dir) / "unknown_points.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict)]


def count_pending_unknowns(job_dir: str | Path) -> int:
    """Count unknowns that still need a human answer (open / asked)."""
    pending = 0
    for item in load_unknown_points(job_dir):
        if _is_answered_unknown(item):
            continue
        pending += 1
    return pending


def has_pending_unknowns(job_dir: str | Path) -> bool:
    return count_pending_unknowns(job_dir) > 0


def build_questions_review_md(job_dir: str | Path, *, job_id: str = "") -> str:
    """Assemble Cursor-facing review MD (prefers integrated ②③+reader export)."""
    job = Path(job_dir)
    jid = job_id or job.name
    resume = f"python reprocess_job.py --job-dir {job} --from-step resume"

    # Prefer full integrated export when edit_proposals or reader_pass exist.
    has_proposals = (job / "edit_proposals.json").is_file()
    has_reader = (job / "reader_pass_result.json").is_file() or (
        job / "reader_pass_questions.md"
    ).is_file()
    if has_proposals or has_reader:
        try:
            from integrated_questions import build_integrated_md, build_cascade_questions
            from integrated_questions import load_ask_proposals, load_reader_pass_findings
            from integrated_questions import load_transcript

            proposals = load_ask_proposals(job)
            findings = load_reader_pass_findings(job)
            cascade = build_cascade_questions(proposals)
            md, _stats = build_integrated_md(
                job_id=jid,
                reader_findings=findings,
                cascade_questions=cascade,
                transcript=load_transcript(job),
                resume_hint=resume,
            )
            return md
        except Exception:
            pass

    sections: list[str] = [
        f"# 質問レビュー（回答待ち）",
        "",
        f"- job: `{jid}`",
        f"- mode: `{resolve_question_mode()}`",
        "",
        "回答記入後、本文を確定してから:",
        "",
        "```",
        resume,
        "```",
        "",
        "---",
        "",
    ]

    reader_md = job / "reader_pass_questions.md"
    if reader_md.is_file():
        sections.append("## Reader pass")
        sections.append("")
        sections.append(reader_md.read_text(encoding="utf-8").strip())
        sections.append("")
        sections.append("---")
        sections.append("")

    q_msg = job / "question_message.txt"
    if q_msg.is_file() and q_msg.read_text(encoding="utf-8").strip():
        sections.append("## 現行 LINE / 質問サイクル（1問）")
        sections.append("")
        sections.append(q_msg.read_text(encoding="utf-8").strip())
        sections.append("")
        sections.append("---")
        sections.append("")

    if len(sections) <= 12:
        sections.append("_質問アーティファクトがまだありません。_")
        sections.append("")

    return "\n".join(sections)


def write_questions_review_md(job_dir: str | Path, *, job_id: str = "") -> Path:
    job = Path(job_dir)
    jid = job_id or job.name
    resume = f"python reprocess_job.py --job-dir {job} --from-step resume"
    has_proposals = (job / "edit_proposals.json").is_file()
    has_reader = (job / "reader_pass_result.json").is_file() or (
        job / "reader_pass_questions.md"
    ).is_file()
    if has_proposals or has_reader:
        try:
            from integrated_questions import write_integrated_questions

            path, _stats = write_integrated_questions(
                job, job_id=jid, resume_hint=resume
            )
            return path
        except Exception:
            pass
    md = build_questions_review_md(job, job_id=jid)
    path = job / QUESTIONS_REVIEW_MD
    path.write_text(md, encoding="utf-8")
    return path
