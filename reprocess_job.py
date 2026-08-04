#!/usr/bin/env python3
"""reprocess_job.py - 完成済みジョブの後段ステップを再適用する公式ツール。

用途: Step 6.x（議事録生成・Google Doc出力）を単独で再実行する。
      機能改善のたびに過去ジョブへ再適用するためのツール。

使い方:
  python reprocess_job.py --job-dir /path/to/job --from-step 6.1
  python reprocess_job.py --job-dir /path/to/job --from-step 6.1 \\
      --input-override /path/to/annotated_ai.txt
  python reprocess_job.py --job-dir /path/to/job --from-step 6.3  # Docs のみ再出力
  python reprocess_job.py --job-dir /path/to/job --from-step resume  # QUESTION_MODE 再開

引数:
  --job-dir         : ジョブディレクトリの絶対パス
  --input-root      : data/transcriptions ルート（省略時: job-dir の親）
  --from-step       : 再実行開始ステップ: 6.1 / 6.2 / 6.3 / resume
  --input-override  : 入力ファイル差し替えパス
                        6.1 始まり → generate_minutes_transcript の --input に渡す
                        6.2 始まり → generate_minutes_other_sections の --input に渡す
                        6.3 始まり → export_minutes_to_google_docs の --input に渡す
  --verify-pattern  : Step 6.3 後 Google Doc 読み戻しで確認する文字列
                        デフォルト: '[補足:'  空文字 "" でスキップ
  --credentials     : Google OAuth credentials.json パス（デフォルト: credentials.json）
  --token           : Google OAuth token.json パス（デフォルト: token.json）
  --docs-chunk-size : Step 6.3 チャンクサイズ（デフォルト: 5000）
  --reason          : reprocess 理由（ログ用、省略可）
  --dry-run         : 実際には実行せず、何をするかを表示するだけ
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_STEPS = ("6.1", "6.2", "6.3")
# resume: QUESTION_MODE pause 後の再開（本文確定済み前提で 6.1 から）
_FROM_STEP_CHOICES = ("6.1", "6.2", "6.3", "resume")
_STEP_ORDER = {s: i for i, s in enumerate(_STEPS)}

_BACKUP_FILES = [
    "merged_transcript_after_qa.txt",
    "minutes_draft.md",
    "minutes_structured.md",
    "google_doc_hub.json",
]


def _log(msg: str) -> None:
    print(f"[reprocess] {msg}", flush=True)


def run_step(cmd: list[str], step: str, *, dry_run: bool = False) -> dict[str, str]:
    """サブプロセスを実行し、key=value 形式の stdout をパースして返す。"""
    if dry_run:
        _log(f"DRY-RUN {step}: {' '.join(cmd)}")
        return {}
    _log(f"{step}: start  cmd={' '.join(cmd)}")
    # Windows 子プロセスが CP932 の警告等を混在させても、出力デコード失敗で
    # 本体処理（特に Google Docs 書き込み）後にラッパーだけ落ちないようにする。
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    if result.returncode != 0:
        raise RuntimeError(
            f"{step} failed (exit={result.returncode})\n"
            f"stdout: {stdout}\nstderr: {stderr}"
        )
    output: dict[str, str] = {}
    for line in stdout.strip().splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            output[k.strip()] = v.strip()
    _log(f"{step}: done  output_keys={list(output.keys())}")
    return output


def backup_job_dir(job_dir: Path, *, dry_run: bool = False) -> Path:
    """バックアップディレクトリを作成して現状の出力ファイルをコピーする。"""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_dir = job_dir / f"_reprocess_backup_{ts}"
    if dry_run:
        _log(f"DRY-RUN backup: would create {backup_dir}")
        return backup_dir
    backup_dir.mkdir(parents=True, exist_ok=True)
    for name in _BACKUP_FILES:
        src = job_dir / name
        if src.exists():
            shutil.copy2(src, backup_dir / name)
            _log(f"backup: {name}")
    return backup_dir


def load_doc_id(job_dir: Path) -> str | None:
    """google_doc_hub.json から doc_id を読む。"""
    hub = job_dir / "google_doc_hub.json"
    if not hub.exists():
        return None
    try:
        return str(json.loads(hub.read_text(encoding="utf-8")).get("doc_id") or "").strip() or None
    except (OSError, json.JSONDecodeError):
        return None


def verify_doc_pattern(
    doc_id: str,
    pattern: str,
    credentials_path: str,
    token_path: str,
) -> dict[str, Any]:
    """Google Docを読み戻してpatternが含まれているか確認する。

    export_minutes_to_google_docs からインポートして認証済みサービスを構築する。
    Step 6.3 が「書き込み成功」と報告しても実際の内容を独立検証する用途。
    """
    from export_minutes_to_google_docs import (
        load_or_create_google_docs_credentials,
        fetch_google_doc_text_with_retry,
    )
    from googleapiclient.discovery import build  # type: ignore

    creds = load_or_create_google_docs_credentials(
        credentials_json_path=credentials_path,
        token_json_path=token_path,
    )
    docs_service = build("docs", "v1", credentials=creds, cache_discovery=False)
    text = fetch_google_doc_text_with_retry(docs_service, doc_id)
    found = pattern in text
    count = text.count(pattern)
    return {
        "pattern": pattern,
        "found": found,
        "count": count,
        "doc_chars": len(text),
    }


def reprocess(
    job_dir: Path,
    *,
    input_root: str,
    from_step: str,
    input_override: Path | None,
    verify_pattern: str,
    credentials: str,
    token: str,
    docs_chunk_size: int,
    reason: str,
    dry_run: bool,
) -> dict[str, Any]:
    """ジョブのステップを再実行し、結果 dict を返す。"""
    py = sys.executable
    repo = str(Path(__file__).parent)
    job_id = job_dir.name

    # resume = QUESTION_MODE pause 後: 本文確定済み前提で Step 6.1 から再開
    resume_from_pause = from_step == "resume"
    if resume_from_pause:
        from_step = "6.1"
        if not reason:
            reason = "resume_after_question_pause"

    doc_id = load_doc_id(job_dir)

    _log(f"job_id={job_id}")
    _log(f"from_step={from_step}  input_root={input_root}")
    _log(f"input_override={input_override}")
    _log(f"doc_id={doc_id}")
    _log(f"dry_run={dry_run}")
    if resume_from_pause:
        _log("resume: clearing question_pause marker and running 6.1–6.3")

    backup_dir = backup_job_dir(job_dir, dry_run=dry_run)

    if resume_from_pause and not dry_run:
        try:
            from question_mode import clear_pause_marker
            from progress_tracker import update_job_progress

            cleared = clear_pause_marker(job_dir)
            _log(f"resume: pause_marker_cleared={cleared}")
            update_job_progress(
                input_root=input_root,
                job_id=job_id,
                phase="resume_after_question_pause",
                status="running",
                detail={"from_step": "6.1"},
                overall_status="running",
            )
        except Exception as e:  # noqa: BLE001
            _log(f"resume: pause clear warning {e!r}")

    steps_run: list[str] = []
    step_outputs: dict[str, Any] = {}
    verify_result: dict[str, Any] = {}

    # --- Step 6.1 ---
    if _STEP_ORDER[from_step] <= _STEP_ORDER["6.1"]:
        cmd = [
            py, os.path.join(repo, "generate_minutes_transcript.py"),
            "--job-id", job_id,
            "--input-root", input_root,
        ]
        if input_override and from_step == "6.1":
            cmd.extend(["--input", str(input_override)])
        out = run_step(cmd, "step_6_1", dry_run=dry_run)
        step_outputs["6.1"] = out
        steps_run.append("6.1")

    # --- Step 6.2 ---
    if _STEP_ORDER[from_step] <= _STEP_ORDER["6.2"]:
        cmd = [
            py, os.path.join(repo, "generate_minutes_other_sections.py"),
            "--job-id", job_id,
            "--input-root", input_root,
        ]
        if input_override and from_step == "6.2":
            cmd.extend(["--input", str(input_override)])
        out = run_step(cmd, "step_6_2", dry_run=dry_run)
        step_outputs["6.2"] = out
        steps_run.append("6.2")

    # --- Step 6.3 ---
    if _STEP_ORDER[from_step] <= _STEP_ORDER["6.3"]:
        if not doc_id and not dry_run:
            raise RuntimeError(
                f"google_doc_hub.json に doc_id がありません。"
                f"Step 6.3 は既存 doc_id が必要です: {job_dir / 'google_doc_hub.json'}"
            )
        hub_meta_path = str(job_dir / "google_doc_hub.json")
        cmd = [
            py, os.path.join(repo, "export_minutes_to_google_docs.py"),
            "--job-id", job_id,
            "--input-root", input_root,
            "--chunk-size", str(docs_chunk_size),
            "--push",
            "--credentials", credentials,
            "--token", token,
        ]
        if doc_id:
            cmd.extend(["--update-doc-id", doc_id, "--write-doc-meta-json", hub_meta_path])
        if input_override and from_step == "6.3":
            cmd.extend(["--input", str(input_override)])
        out = run_step(cmd, "step_6_3", dry_run=dry_run)
        step_outputs["6.3"] = out
        steps_run.append("6.3")
        _log(f"step 6.3: full_write_verified={out.get('full_write_verified')}")

        # Google Doc 読み戻し検証（APIの成功応答だけで判定しない）
        if verify_pattern and not dry_run and doc_id:
            try:
                verify_result = verify_doc_pattern(doc_id, verify_pattern, credentials, token)
                status = "OK" if verify_result["found"] else "NOT FOUND"
                _log(
                    f"verify [{status}]: pattern='{verify_pattern}' "
                    f"count={verify_result['count']} "
                    f"doc_chars={verify_result['doc_chars']}"
                )
                if not verify_result["found"]:
                    _log("WARNING: verify_pattern が Google Doc に見つかりません。")
            except Exception as e:
                verify_result = {"pattern": verify_pattern, "found": None, "error": str(e)}
                _log(f"verify: error {e!r}")
        elif dry_run:
            verify_result = {"pattern": verify_pattern, "dry_run": True}

    log_entry: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "reason": reason or "",
        "from_step": "resume" if resume_from_pause else from_step,
        "input_override": str(input_override) if input_override else None,
        "backup_dir": str(backup_dir),
        "steps_run": steps_run,
        "step_outputs": step_outputs,
        "verify": verify_result,
        "dry_run": dry_run,
        "resume_from_pause": resume_from_pause,
    }

    if not dry_run:
        log_path = job_dir / "_reprocess_log.jsonl"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
        _log(f"log → {log_path.name}")
        if resume_from_pause:
            try:
                from progress_tracker import finalize_job_progress

                finalize_job_progress(
                    input_root=input_root,
                    job_id=job_id,
                    overall_status="success",
                )
                _log("resume: overall_status=success")
            except Exception as e:  # noqa: BLE001
                _log(f"resume: finalize warning {e!r}")

    return log_entry


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="完成済みジョブに後段ステップを再適用する",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--job-dir", required=True, help="ジョブディレクトリの絶対パス")
    parser.add_argument(
        "--input-root",
        default=None,
        help="data/transcriptions ルート（省略時: job-dir の親ディレクトリ）",
    )
    parser.add_argument(
        "--from-step",
        required=True,
        choices=list(_FROM_STEP_CHOICES),
        help="再実行開始ステップ: 6.1 / 6.2 / 6.3 / resume（QUESTION_MODE 一時停止後）",
    )
    parser.add_argument(
        "--input-override",
        default=None,
        help="from-step への入力ファイルを差し替えるパス（省略時: 既存ファイルをそのまま使用）",
    )
    parser.add_argument(
        "--verify-pattern",
        default="[補足:",
        help="Step 6.3 後の Google Doc 読み戻し検証文字列（デフォルト: '[補足:'、空文字でスキップ）",
    )
    parser.add_argument(
        "--credentials",
        default="credentials.json",
        help="Google OAuth credentials.json パス（デフォルト: credentials.json）",
    )
    parser.add_argument(
        "--token",
        default="token.json",
        help="Google OAuth token.json パス（デフォルト: token.json）",
    )
    parser.add_argument(
        "--docs-chunk-size",
        type=int,
        default=5000,
        help="Step 6.3 チャンクサイズ（デフォルト: 5000）",
    )
    parser.add_argument("--reason", default="", help="reprocess 理由（ログ用）")
    parser.add_argument("--dry-run", action="store_true", help="実行せず内容を表示するだけ")
    args = parser.parse_args()

    job_dir = Path(args.job_dir)
    if not job_dir.is_dir():
        print(f"ERROR: job-dir が見つかりません: {job_dir}", file=sys.stderr)
        return 1

    input_root = args.input_root or str(job_dir.parent)

    input_override: Path | None = None
    if args.input_override:
        input_override = Path(args.input_override)
        if not args.dry_run and not input_override.exists():
            print(f"ERROR: input-override が見つかりません: {input_override}", file=sys.stderr)
            return 1

    result = reprocess(
        job_dir,
        input_root=input_root,
        from_step=args.from_step,
        input_override=input_override,
        verify_pattern=args.verify_pattern,
        credentials=args.credentials,
        token=args.token,
        docs_chunk_size=args.docs_chunk_size,
        reason=args.reason,
        dry_run=args.dry_run,
    )

    print()
    print("=== reprocess 結果 ===")
    print(f"steps_run:   {result['steps_run']}")
    if result.get("verify"):
        v = result["verify"]
        if v.get("dry_run"):
            print(f"verify:      DRY-RUN (pattern='{v['pattern']}')")
        elif v.get("error"):
            print(f"verify:      ERROR {v['error']}")
        else:
            status = "OK" if v.get("found") else "NOT FOUND"
            print(f"verify:      [{status}] pattern='{v.get('pattern')}' count={v.get('count')} doc_chars={v.get('doc_chars')}")
    if not result["dry_run"]:
        print(f"log:         {job_dir / '_reprocess_log.jsonl'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
