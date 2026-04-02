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

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))


def _py() -> str:
    return sys.executable


def _run(args: list[str], cwd: str | None = None) -> None:
    cmd_text = " ".join(args)
    print(f"[run_docs_hub_e2e] {cmd_text}", flush=True)
    r = subprocess.run(args, cwd=cwd or REPO_ROOT)
    if r.returncode != 0:
        raise SystemExit(r.returncode)


def _line_push_env_ready() -> bool:
    user_id = os.getenv("LINE_USER_ID", "").strip()
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    return bool(user_id and token)


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


def _export_cmd(
    job_id: str,
    input_root: str,
    *,
    push: bool,
    chunk_size: int,
    credentials: str,
    token: str,
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
        "--credentials",
        credentials,
        "--token",
        token,
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


def cmd_sync_docs(args: argparse.Namespace) -> None:
    if not getattr(args, "skip_compose", False):
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
    _run(
        _export_cmd(
            args.job_id,
            args.input_root,
            push=args.push,
            chunk_size=args.chunk_size,
            credentials=args.credentials,
            token=args.token,
            drive_parent=args.drive_parent_folder_id,
            drive_subfolder=args.drive_subfolder_name,
            title=args.title,
        )
    )


def cmd_after_answer(args: argparse.Namespace) -> None:
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
        ]
    )
    _run(
        [
            _py(),
            os.path.join(REPO_ROOT, "generate_minutes_transcript.py"),
            "--job-id",
            args.job_id,
            "--input-root",
            args.input_root,
        ]
    )
    # 回答反映後の全文から unknown_points を再評価し、必要なら「次の1問」を生成する。
    after_qa_path = os.path.join(
        args.input_root,
        args.job_id,
        "merged_transcript_after_qa.txt",
    )
    unknowns_path = os.path.join(args.input_root, args.job_id, "unknown_points.json")
    _run(
        [
            _py(),
            os.path.join(REPO_ROOT, "refresh_unknown_points_after_answer.py"),
            "--job-id",
            args.job_id,
            "--input-root",
            args.input_root,
            "--answers-json",
            args.answers_json,
            "--input",
            after_qa_path,
        ]
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
    _run(qcycle_cmd)
    cmd_sync_docs(args)


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
    parser.add_argument("--credentials", default="credentials.json")
    parser.add_argument("--token", default="token.json")
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
        default=8,
        help="次質問を出す最小value（デフォルト: 8）",
    )
    parser.add_argument(
        "--include-internal-workspace",
        action="store_true",
        help="Docs 用 MD に確認ワークスペース・回答メタを含める（デフォルトは成果物のみ）",
    )
    parser.add_argument(
        "--skip-compose",
        action="store_true",
        help="compose_docs_hub_markdown.py をスキップし、既存の minutes_structured.md をそのまま使用する（generate_minutes_other_sections.py 実行後に使用）",
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
