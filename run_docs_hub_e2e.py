"""
Docs ハブ一括: 発言録本文（＋任意テンプレ）を Google Docs に出し、同一 job_id で更新する薄いオーケストレータ。

デフォルトの Markdown には確認質問・ユーザー回答を載せない（回答は recorrect で本文に反映済みである前提）。
社内デバッグ用に確認ワークスペースを戻す場合は --include-internal-workspace または
環境変数 DOCS_HUB_INCLUDE_INTERNAL_WORKSPACE=1。

前提:
  - run_job_once.py まで完了している（または --after-answer 用に transcript / question がある）
  - 初回 Docs 作成後は data/transcriptions/<job_id>/google_doc_hub.json に doc_id を保存
"""
import argparse
import json
import os
import subprocess
import sys

from meeting_profile import infer_display_title, load_meeting_profile, resolve_display_title

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))


def _py() -> str:
    return sys.executable


def _run(args: list[str], cwd: str | None = None, log_path: str | None = None) -> None:
    cmd_text = " ".join(args)
    print(f"[run_docs_hub_e2e] {cmd_text}", flush=True)
    r = subprocess.run(
        args,
        cwd=cwd or REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if r.stdout:
        print(r.stdout, end="", flush=True)
    if r.stderr:
        print(r.stderr, end="", file=sys.stderr, flush=True)
    if log_path:
        from run_job_once import log_line

        step = os.path.basename(args[1]) if len(args) > 1 else "cmd"
        if r.stdout and r.stdout.strip():
            log_line(log_path, f"{step}: stdout\n{r.stdout.rstrip()}")
        if r.stderr and r.stderr.strip():
            log_line(log_path, f"{step}: stderr\n{r.stderr.rstrip()}")
        log_line(log_path, f"{step}: exit_code={r.returncode}")
    if r.returncode != 0:
        raise SystemExit(r.returncode)


def _line_push_env_ready() -> bool:
    user_id = os.getenv("LINE_USER_ID", "").strip()
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    return bool(user_id and token)


def _resolve_answers_json(job_id: str, input_root: str, explicit: str | None) -> str:
    if explicit and os.path.isfile(explicit):
        return explicit
    job_answers = os.path.join(input_root, job_id, "answers.json")
    if os.path.isfile(job_answers):
        return job_answers
    return explicit or os.path.join("data", "line_answers.json")


def _meta_path(job_id: str, input_root: str) -> str:
    return os.path.join(input_root, job_id, "google_doc_hub.json")


def _load_doc_id(job_id: str, input_root: str) -> str | None:
    p = _meta_path(job_id, input_root)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        s = str(data.get("doc_id") or "").strip()
        return s or None
    except (OSError, json.JSONDecodeError):
        return None


def _resolve_export_title(job_id: str, input_root: str, cli_title: str | None) -> str | None:
    if cli_title:
        return cli_title
    job_dir = os.path.join(input_root, job_id)
    profile = load_meeting_profile(job_dir)
    resolved = resolve_display_title(profile, job_id=job_id)
    if resolved != job_id:
        return resolved
    inferred = infer_display_title(job_dir, job_id)
    return inferred if inferred != job_id else None


def _export_cmd(
    job_id: str,
    input_root: str,
    *,
    push: bool,
    chunk_size: int,
    drive_parent: str | None,
    drive_subfolder: str | None,
    title: str | None,
) -> list[str]:
    doc_id = _load_doc_id(job_id, input_root)
    meta = _meta_path(job_id, input_root)
    cmd = [
        _py(),
        os.path.join(REPO_ROOT, "export_minutes_to_google_docs.py"),
        "--job-id",
        job_id,
        "--input-root",
        input_root,
        "--chunk-size",
        str(chunk_size),
        "--write-doc-meta-json",
        meta,
    ]
    if title:
        cmd.extend(["--title", title])
    if drive_parent:
        cmd.extend(["--drive-parent-folder-id", drive_parent])
        if drive_subfolder:
            cmd.extend(["--drive-subfolder-name", drive_subfolder])
    if push:
        cmd.append("--push")
        if doc_id:
            cmd.extend(["--update-doc-id", doc_id])
    return cmd


def _minutes_structured_path(job_id: str, input_root: str) -> str:
    return os.path.join(input_root, job_id, "minutes_structured.md")


def cmd_sync_docs(args: argparse.Namespace) -> None:
    structured_md = _minutes_structured_path(args.job_id, args.input_root)
    if getattr(args, "compose_from_scratch", False):
        compose_cmd = [
            _py(),
            os.path.join(REPO_ROOT, "compose_docs_hub_markdown.py"),
            "--job-id",
            args.job_id,
            "--input-root",
            args.input_root,
            "--answers-json",
            args.answers_json,
        ]
        if getattr(args, "include_internal_workspace", False):
            compose_cmd.append("--include-internal-workspace")
        _run(compose_cmd + (["--title", args.title] if args.title else []))
    elif not os.path.isfile(structured_md):
        raise FileNotFoundError(
            f"minutes_structured.md not found: {structured_md}. "
            "Run generate_minutes_other_sections.py first, or pass --compose-from-scratch "
            "for legacy jobs (note: compose omits 論点メモ)."
        )
    export_title = _resolve_export_title(args.job_id, args.input_root, args.title)
    _run(
        _export_cmd(
            args.job_id,
            args.input_root,
            push=args.push,
            chunk_size=args.chunk_size,
            drive_parent=args.drive_parent_folder_id,
            drive_subfolder=args.drive_subfolder_name,
            title=export_title,
        )
    )


def cmd_after_answer(args: argparse.Namespace) -> None:
    from progress_tracker import wait_for_job_pipeline_idle
    from run_job_once import record_visible_progress

    wait_sec = float(os.getenv("AFTER_ANSWER_PIPELINE_WAIT_SEC", "3600"))
    print(
        f"[run_docs_hub_e2e] waiting for main pipeline idle "
        f"(job_id={args.job_id} timeout_sec={wait_sec})",
        flush=True,
    )
    if not wait_for_job_pipeline_idle(
        args.input_root, args.job_id, timeout_sec=wait_sec
    ):
        print(
            "[run_docs_hub_e2e] WARN: pipeline did not become idle before after-answer; "
            "Docs update may race with step_6_3",
            flush=True,
        )
    else:
        print("[run_docs_hub_e2e] main pipeline idle; starting after-answer", flush=True)

    job_dir = os.path.join(args.input_root, args.job_id)
    log_path = os.path.join(job_dir, "e2e_run_log.txt")
    visible_log_path = os.path.join(job_dir, "processing_visible_log.txt")
    answers_json = _resolve_answers_json(
        args.job_id, args.input_root, args.answers_json
    )
    record_visible_progress(
        log_path=log_path,
        visible_log_path=visible_log_path,
        job_id=args.job_id,
        message="回答反映: 逐語録への反映を開始します",
    )
    _run(
        [
            _py(),
            os.path.join(REPO_ROOT, "recorrect_from_line_answer.py"),
            "--job-id",
            args.job_id,
            "--input-root",
            args.input_root,
            "--answers-json",
            answers_json,
        ],
        log_path=log_path,
    )
    from run_job_once import ensure_after_qa_exists

    ensure_after_qa_exists(job_dir, log_path)
    from transcript_paths import resolve_transcript_path_for_minutes

    after_qa_path = resolve_transcript_path_for_minutes(
        args.job_id, None, args.input_root
    )
    _run(
        [
            _py(),
            os.path.join(REPO_ROOT, "generate_minutes_transcript.py"),
            "--job-id",
            args.job_id,
            "--input-root",
            args.input_root,
        ],
        log_path=log_path,
    )
    # 回答反映後の全文から unknown_points を再評価し、必要なら「次の1問」を生成する。
    unknowns_path = os.path.join(args.input_root, args.job_id, "unknown_points.json")
    record_visible_progress(
        log_path=log_path,
        visible_log_path=visible_log_path,
        job_id=args.job_id,
        message="不明点を再評価し、次のLINE質問を選定しています...",
    )
    _run(
        [
            _py(),
            os.path.join(REPO_ROOT, "refresh_unknown_points_after_answer.py"),
            "--job-id",
            args.job_id,
            "--input-root",
            args.input_root,
            "--answers-json",
            answers_json,
            "--input",
            after_qa_path,
        ],
        log_path=log_path,
    )
    qcycle_cmd = [
        _py(),
        os.path.join(REPO_ROOT, "run_question_cycle_once.py"),
        "--job-id",
        args.job_id,
        "--input-root",
        args.input_root,
        "--unknowns",
        unknowns_path,
        "--text",
        after_qa_path,
        "--min-question-value",
        str(args.min_question_value),
    ]
    send_line_enabled = bool(args.send_line or _line_push_env_ready())
    print(f"[run_docs_hub_e2e] send_line_enabled={send_line_enabled}", flush=True)
    if send_line_enabled:
        qcycle_cmd.append("--send-line")
    _run(qcycle_cmd, log_path=log_path)
    record_visible_progress(
        log_path=log_path,
        visible_log_path=visible_log_path,
        job_id=args.job_id,
        message="質問の選定とLINE通知が完了しました",
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
        log_path=log_path,
    )
    args.skip_compose = True
    cmd_sync_docs(args)
    question_result_path = os.path.join(args.input_root, args.job_id, "question_result.json")
    try:
        with open(question_result_path, encoding="utf-8") as f:
            qresult = json.load(f)
        if str(qresult.get("question_status") or "") != "generated":
            from run_job_once import update_doc_title_from_hub

            export_title = _resolve_export_title(args.job_id, args.input_root, args.title)
            if export_title:
                update_doc_title_from_hub(
                    _meta_path(args.job_id, args.input_root),
                    f"【処理完了】{export_title}",
                    log_path,
                )
                record_visible_progress(
                    log_path=log_path,
                    visible_log_path=visible_log_path,
                    job_id=args.job_id,
                    message=f"Googleドキュメント名を【処理完了】に更新しました",
                )
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    record_visible_progress(
        log_path=log_path,
        visible_log_path=visible_log_path,
        job_id=args.job_id,
        message="===== 回答反映サイクルが完了しました =====",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Docs ハブ: compose + Google Docs 作成/更新、または回答後の再補正まで"
    )
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--input-root", default="data/transcriptions")
    parser.add_argument(
        "--answers-json",
        default=os.path.join("data", "line_answers.json"),
    )
    parser.add_argument(
        "--after-answer",
        action="store_true",
        help="recorrect → generate_minutes_transcript → compose → Docs 更新",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="Google Docs へ実際に書き込む（未指定時は export の dry-run のみ）",
    )
    parser.add_argument("--chunk-size", type=int, default=5000)
    parser.add_argument("--drive-parent-folder-id", default=None)
    parser.add_argument("--drive-subfolder-name", default=None)
    parser.add_argument("--title", default=None, help="Docs タイトル（未指定時は job_id）")
    parser.add_argument(
        "--send-line",
        action="store_true",
        help="after-answer 時に次の質問を LINE 送信する",
    )
    parser.add_argument(
        "--min-question-value",
        type=int,
        default=7,
        help="次質問を出す最小value（デフォルト: 7）",
    )
    parser.add_argument(
        "--include-internal-workspace",
        action="store_true",
        help="Docs 用 MD に確認ワークスペース・回答メタを含める（デフォルトは成果物のみ）",
    )
    parser.add_argument(
        "--compose-from-scratch",
        action="store_true",
        help=(
            "compose_docs_hub_markdown.py で minutes_structured.md を再生成する。"
            "Phase 6 以降の「論点メモ」は含まれない旧形式のため、通常は指定しない。"
        ),
    )
    parser.add_argument(
        "--skip-compose",
        action="store_true",
        help="（非推奨・互換用）デフォルトで compose はスキップされるため指定不要",
    )
    args = parser.parse_args()
    if os.getenv("DOCS_HUB_INCLUDE_INTERNAL_WORKSPACE", "").strip().lower() in ("1", "true", "yes"):
        args.include_internal_workspace = True

    if args.after_answer:
        cmd_after_answer(args)
    else:
        cmd_sync_docs(args)

    meta = _meta_path(args.job_id, args.input_root)
    print(f"job_id={args.job_id}")
    print(f"google_doc_hub_meta={meta}")
    if os.path.isfile(meta):
        with open(meta, "r", encoding="utf-8") as f:
            hub = json.load(f)
        print(f"doc_url={hub.get('doc_url', '')}")
    print("status=docs_hub_e2e_done")


if __name__ == "__main__":
    main()
