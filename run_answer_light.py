#!/usr/bin/env python3
"""QUESTION_MODE=cursor/line 用の軽量回答反映。

回答ごとに議事録再生成・Docs 全更新はしない。
  1. recorrect_from_line_answer（本文へ pinpoint / batch 反映）
  2. pending が残る → 次の質問を生成（必要なら LINE）し paused のまま
  3. pending が 0 → reprocess_job --from-step resume（6.1–6.3 を1回だけ）
     → 完了 LINE（[完了]）を送る
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

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


def _load_doc_url(job_dir: str | Path) -> str:
    hub = Path(job_dir) / "google_doc_hub.json"
    if not hub.is_file():
        return ""
    try:
        data = json.loads(hub.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    url = str(data.get("doc_url") or "").strip()
    if url:
        return url
    doc_id = str(data.get("doc_id") or "").strip()
    if doc_id:
        return f"https://docs.google.com/document/d/{doc_id}/edit"
    return ""


def send_completion_line(
    job_dir: str | Path,
    *,
    job_id: str,
    send_line: bool,
) -> dict:
    """全回答反映・議事録更新後の完了通知（従来 after-answer と同じ [完了] 形式）。"""
    from line_send_question import build_line_message, push_line_message

    job = Path(job_dir)
    doc_url = _load_doc_url(job)
    payload = {
        "job_id": job_id,
        "question_status": "none",
        "completion_kind": "full",
        "message": (
            "回答の反映と議事録の更新が完了しました。"
            "追加の確認事項はありません。"
        ),
        "selected_unknown": None,
        "doc_url": doc_url,
        "question_text": "",
    }
    q_path = job / "question_result.json"
    msg_path = job / "question_message.txt"
    q_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    message_text = build_line_message(payload)
    msg_path.write_text(message_text, encoding="utf-8")

    result: dict = {
        "sent": False,
        "reason": "written_only",
        "message": message_text,
    }
    if not send_line:
        return result
    user_id = os.getenv("LINE_USER_ID", "").strip()
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if not user_id or not token:
        result["reason"] = "line_credentials_missing"
        return result
    try:
        push_line_message(
            channel_access_token=token,
            user_id=user_id,
            text=message_text,
        )
        result["sent"] = True
        result["reason"] = "sent_completion"
    except Exception as e:  # noqa: BLE001
        result["reason"] = f"push_failed:{e!r}"
        print(f"[answer_light] completion_line_failed={e!r}", flush=True)
    return result


def _mark_doc_completed(job_dir: str, job_id: str, input_root: str, log_path: str) -> None:
    """Doc タイトルを【処理完了】に戻す（best-effort）。"""
    try:
        from run_job_once import update_doc_title_from_hub

        hub = os.path.join(job_dir, "google_doc_hub.json")
        title = job_id
        # display title: strip job_ prefix if present
        if job_id.startswith("job_") and "_" in job_id[4:]:
            # job_YYYYMMDD_HHMMSS_rest
            parts = job_id.split("_", 3)
            if len(parts) >= 4:
                title = parts[3]
        update_doc_title_from_hub(hub, f"【処理完了】{title}", log_path)
    except Exception as e:  # noqa: BLE001
        print(f"[answer_light] doc_title_update_failed={e!r}", flush=True)


def _question_cycle_generated_new_question(job_dir: str | Path) -> bool:
    """直近の question_cycle が新規質問を生成したか。"""
    qr = Path(job_dir) / "question_result.json"
    if not qr.is_file():
        return False
    try:
        data = json.loads(qr.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    return str(data.get("question_status") or "").strip() == "generated"


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
        send_line = bool(args.send_line or (should_send_line() and _line_push_env_ready()))
        if send_line:
            try:
                from integrated_questions import send_deferred_line_bundle_if_any

                deferred = send_deferred_line_bundle_if_any(job_dir, send_line=True)
                if deferred.get("sent"):
                    print(
                        "[answer_light] deferred_bundle sent "
                        f"targets={deferred.get('target_count')}",
                        flush=True,
                    )
                    update_job_progress(
                        input_root=args.input_root,
                        job_id=args.job_id,
                        phase="question_pause",
                        status="success",
                        detail={"pending_unknowns": pending, "deferred_bundle": deferred},
                        overall_status="paused",
                    )
                    return 0
            except Exception as e:  # noqa: BLE001
                print(f"[answer_light] deferred_bundle_failed={e!r}", flush=True)
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
        if rc != 0:
            update_job_progress(
                input_root=args.input_root,
                job_id=args.job_id,
                phase="question_pause",
                status="failed",
                detail={"pending_unknowns": pending, "next_question_exit": rc},
                overall_status="paused",
            )
            return rc
        if _question_cycle_generated_new_question(job_dir):
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
                status="success",
                detail={"pending_unknowns": pending, "next_question_exit": rc},
                overall_status="paused",
            )
            print(f"[answer_light] status=paused pending={pending}", flush=True)
            return 0
        print(
            "[answer_light] question_cycle returned no new question; "
            "completing pipeline (threshold skip / no candidates)",
            flush=True,
        )
        # fall through → resume 6.1–6.3

    # 3) All answered or no further questions → resume minutes once
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
        _mark_doc_completed(job_dir, args.job_id, args.input_root, log_path)
        send_line = bool(args.send_line or (should_send_line() and _line_push_env_ready()))
        completion = send_completion_line(
            job_dir,
            job_id=args.job_id,
            send_line=send_line,
        )
        print(
            f"[answer_light] completion_line sent={completion.get('sent')} "
            f"reason={completion.get('reason')}",
            flush=True,
        )
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(
                    f"completion_line sent={completion.get('sent')} "
                    f"reason={completion.get('reason')}\n"
                )
        except OSError:
            pass
        update_job_progress(
            input_root=args.input_root,
            job_id=args.job_id,
            phase="done",
            status="success",
            detail={
                "pending_unknowns": 0,
                "completion_line": completion.get("reason"),
            },
            overall_status="success",
        )
        print("[answer_light] status=success", flush=True)
    else:
        # resume 失敗もユーザーに分かるよう LINE で通知
        send_line = bool(args.send_line or (should_send_line() and _line_push_env_ready()))
        if send_line and _line_push_env_ready():
            try:
                from line_send_question import push_line_message

                push_line_message(
                    channel_access_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip(),
                    user_id=os.getenv("LINE_USER_ID", "").strip(),
                    text=(
                        "[エラー] 回答の反映後、議事録の更新に失敗しました。"
                        "ログを確認してください。"
                    ),
                )
            except Exception as e:  # noqa: BLE001
                print(f"[answer_light] error_line_failed={e!r}", flush=True)
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
