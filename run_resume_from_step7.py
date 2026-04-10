import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from ai_correct_text import correct_full_text, get_last_correct_full_text_meta
from accumulate_knowledge_step17 import accumulate_knowledge
from detect_unknown_points import detect_unknown_points
from filename_hints import extract_filename_hints
from job_context import load_job_context
from progress_tracker import finalize_job_progress, update_job_progress
from repo_env import load_dotenv_local
from run_job_once import (
    _load_unknown_points_file,
    _save_unknown_points_file,
    append_log_to_drive,
    merge_unknown_points,
    record_visible_progress,
    restore_known_statuses,
    update_doc_title_from_hub,
)


def _run_cmd(log_path: str, args: list[str], step: str) -> None:
    cmd_text = " ".join(args)
    print(f"[run_resume_from_step7] {step}: start cmd={cmd_text}", flush=True)
    completed = subprocess.run(args, capture_output=True, text=True)
    if completed.stdout.strip():
        print(completed.stdout.rstrip(), flush=True)
    if completed.stderr.strip():
        print(completed.stderr.rstrip(), flush=True)
    if completed.returncode != 0:
        raise RuntimeError(f"{step} failed: exit_code={completed.returncode}")


def _line_push_env_ready() -> bool:
    user_id = os.getenv("LINE_USER_ID", "").strip()
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    return bool(user_id and token)


def _strip_status_prefix(title: str) -> str:
    s = str(title or "").strip()
    return re.sub(r"^【[^】]+】", "", s).strip()


def _load_final_doc_title(hub_meta_path: str, fallback: str) -> str:
    if not os.path.isfile(hub_meta_path):
        return fallback
    try:
        with open(hub_meta_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return fallback
        title = _strip_status_prefix(str(data.get("title") or "").strip())
        return title or fallback
    except (OSError, json.JSONDecodeError):
        return fallback


def main() -> None:
    load_dotenv_local()
    parser = argparse.ArgumentParser(description="修正依頼後に Step 7 から再開する")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--input-root", default="data/transcriptions")
    parser.add_argument(
        "--answers-json",
        default=os.path.join("data", "line_answers.json"),
    )
    parser.add_argument("--push", action="store_true", help="Docs を更新する")
    parser.add_argument("--send-line", action="store_true", help="次質問を LINE 送信する")
    parser.add_argument("--min-question-value", type=int, default=8)
    parser.add_argument(
        "--incorporate-latest-answer",
        action="store_true",
        help="最新回答もあわせて本文へ反映してから再開する",
    )
    args = parser.parse_args()

    job_dir = os.path.join(args.input_root, args.job_id)
    if not os.path.isdir(job_dir):
        raise FileNotFoundError(f"job dir not found: {job_dir}")

    merged_path = os.path.join(job_dir, "merged_transcript.txt")
    if not os.path.isfile(merged_path):
        raise FileNotFoundError(f"merged transcript not found: {merged_path}")

    mechanical_path = os.path.join(job_dir, "merged_transcript_mechanical.txt")
    ai_path = os.path.join(job_dir, "merged_transcript_ai.txt")
    after_qa_path = os.path.join(job_dir, "merged_transcript_after_qa.txt")
    unknowns_path = os.path.join(job_dir, "unknown_points.json")
    regex_unknowns_path = os.path.join(job_dir, "unknown_points_regex.json")
    hub_meta_path = os.path.join(job_dir, "google_doc_hub.json")
    log_path = os.path.join(job_dir, "e2e_run_log.txt")
    visible_log_path = os.path.join(job_dir, "processing_visible_log.txt")
    repo_root = os.path.dirname(os.path.abspath(__file__))
    py = sys.executable
    stem = Path(args.job_id).name
    final_doc_title = _load_final_doc_title(hub_meta_path, fallback=stem)

    current_phase = "step_7_mechanical_correct"
    current_step_label = "Step 7: 機械補正"

    try:
        update_job_progress(
            input_root=args.input_root,
            job_id=args.job_id,
            phase=current_phase,
            status="running",
            detail={"resume_reason": "line_correction_request"},
        )
        update_doc_title_from_hub(hub_meta_path, f"【機械補正中】{stem}", log_path)
        record_visible_progress(
            log_path=log_path,
            visible_log_path=visible_log_path,
            job_id=args.job_id,
            message="再補正: 機械補正を実行中...（フィラー除去・句読点整理・相槌圧縮）",
        )
        _run_cmd(
            log_path,
            [
                py,
                os.path.join(repo_root, "mechanical_correct_text.py"),
                "--input",
                merged_path,
                "--output",
                mechanical_path,
            ],
            "step_7_mechanical_correct",
        )
        with open(merged_path, "r", encoding="utf-8") as f:
            raw_text = f.read()
        with open(mechanical_path, "r", encoding="utf-8") as f:
            mechanical_text = f.read()
        update_job_progress(
            input_root=args.input_root,
            job_id=args.job_id,
            phase=current_phase,
            status="success",
            detail={"output_chars": len(mechanical_text)},
        )
        record_visible_progress(
            log_path=log_path,
            visible_log_path=visible_log_path,
            job_id=args.job_id,
            message=(
                f"再補正: 機械補正が完了しました（{len(raw_text):,}文字 → {len(mechanical_text):,}文字、"
                f"{len(raw_text) - len(mechanical_text):,}文字削減）"
            ),
        )

        current_phase = "step_8_9_ai_correct_and_unknowns"
        current_step_label = "Step 8-9: AI補正/AI不明点検出"
        update_job_progress(
            input_root=args.input_root,
            job_id=args.job_id,
            phase=current_phase,
            status="running",
            detail={"input_chars": len(mechanical_text)},
        )
        update_doc_title_from_hub(hub_meta_path, f"【AI補正中】{stem}", log_path)
        record_visible_progress(
            log_path=log_path,
            visible_log_path=visible_log_path,
            job_id=args.job_id,
            message=(
                f"再補正: AIによるテキスト補正を開始します（{len(mechanical_text):,}文字を処理、数分〜十数分かかります）"
            ),
        )

        phase_labels = {
            "ai_correct": "AI補正",
        }

        def _on_ai_phase(phase: str) -> None:
            label = phase_labels.get(phase, phase)
            update_doc_title_from_hub(hub_meta_path, f"【AI補正中：{label}】{stem}", log_path)
            record_visible_progress(
                log_path=log_path,
                visible_log_path=visible_log_path,
                job_id=args.job_id,
                message=f"  AI処理中...（{label}を実行しています）",
            )

        def _on_ai_stream_progress(msg: str) -> None:
            append_log_to_drive(args.job_id, msg)

        hints = extract_filename_hints(args.job_id)
        job_context = load_job_context(job_dir)
        ai_text = correct_full_text(
            text=mechanical_text,
            on_phase=_on_ai_phase,
            filename_hints=hints,
            visible_log_path=visible_log_path,
            job_context=job_context if job_context else None,
            on_stream_progress=_on_ai_stream_progress,
        )
        with open(ai_path, "w", encoding="utf-8") as f:
            f.write(ai_text)
        shutil.copyfile(ai_path, after_qa_path)
        correction_meta = get_last_correct_full_text_meta()
        stop_reason = correction_meta.get("fallback_reason") or correction_meta.get("stop_reason") or "unknown"
        record_visible_progress(
            log_path=log_path,
            visible_log_path=visible_log_path,
            job_id=args.job_id,
            message=f"再補正: AI補正が完了しました（{len(mechanical_text):,}文字 → {len(ai_text):,}文字）",
        )

        # Step 9: AI不明点検出（Claude Opus）
        # 再補正サイクルでも過去の回答済み不明点のステータスを保全するため、
        # 検出前に既存ファイルを読み込む
        old_unknown_points = _load_unknown_points_file(unknowns_path)
        answered_items = [
            item for item in old_unknown_points
            if str(item.get("status", "")).strip().lower() in {"answered", "done", "closed", "resolved"}
        ]
        ai_unknown_points = detect_unknown_points(
            ai_text,
            filename_hints=hints,
            job_context=job_context if job_context else None,
            answered_items=answered_items if answered_items else None,
            visible_log_path=visible_log_path,
        )
        update_job_progress(
            input_root=args.input_root,
            job_id=args.job_id,
            phase=current_phase,
            status="success",
            detail={"output_chars": len(ai_text), "ai_unknowns": len(ai_unknown_points)},
        )
        record_visible_progress(
            log_path=log_path,
            visible_log_path=visible_log_path,
            job_id=args.job_id,
            message=f"再補正: AIによる不明点の検出が完了しました（{len(ai_unknown_points)}件を発見）",
        )
        update_doc_title_from_hub(hub_meta_path, f"【AI補正完了】{stem}", log_path)

        current_phase = "step_10_hybrid_unknowns"
        current_step_label = "Step 10: ハイブリッド不明点検出"
        update_job_progress(
            input_root=args.input_root,
            job_id=args.job_id,
            phase=current_phase,
            status="running",
            detail={},
        )
        record_visible_progress(
            log_path=log_path,
            visible_log_path=visible_log_path,
            job_id=args.job_id,
            message="再補正: パターン照合で追加の不明点を検出しています...",
        )
        _run_cmd(
            log_path,
            [
                py,
                os.path.join(repo_root, "extract_unknown_points.py"),
                "--input",
                ai_path,
                "--output",
                regex_unknowns_path,
            ],
            "step_10_hybrid_unknowns",
        )
        regex_unknown_points = _load_unknown_points_file(regex_unknowns_path)
        merged_unknown_points = merge_unknown_points(
            ai_unknowns=ai_unknown_points,
            regex_unknowns=regex_unknown_points,
        )
        # 再補正サイクルでも過去のQ&A回答・askedステータスを失わないよう復元する
        if old_unknown_points:
            merged_unknown_points = restore_known_statuses(merged_unknown_points, old_unknown_points)
        _save_unknown_points_file(unknowns_path, merged_unknown_points)
        update_job_progress(
            input_root=args.input_root,
            job_id=args.job_id,
            phase=current_phase,
            status="success",
            detail={"total_unknowns": len(merged_unknown_points)},
        )
        record_visible_progress(
            log_path=log_path,
            visible_log_path=visible_log_path,
            job_id=args.job_id,
            message=(
                f"再補正: 不明点検出が完了しました（AI: {len(ai_unknown_points)}件 + パターン: {len(regex_unknown_points)}件"
                f" → 統合: {len(merged_unknown_points)}件）"
            ),
        )
        update_doc_title_from_hub(hub_meta_path, f"【不明点検出完了】{stem}", log_path)

        if args.incorporate_latest_answer and os.path.isfile(args.answers_json):
            current_phase = "step_16_17_apply_latest_answer"
            current_step_label = "Step 16-17: 回答内容反映"
            update_job_progress(
                input_root=args.input_root,
                job_id=args.job_id,
                phase=current_phase,
                status="running",
                detail={},
            )
            record_visible_progress(
                log_path=log_path,
                visible_log_path=visible_log_path,
                job_id=args.job_id,
                message="LINE回答の内容を逐語録に反映しています...",
            )
            _run_cmd(
                log_path,
                [
                    py,
                    os.path.join(repo_root, "recorrect_from_line_answer.py"),
                    "--job-id",
                    args.job_id,
                    "--input-root",
                    args.input_root,
                    "--answers-json",
                    args.answers_json,
                    "--input",
                    ai_path,
                    "--output",
                    after_qa_path,
                ],
                "step_16_apply_latest_answer",
            )
            _run_cmd(
                log_path,
                [
                    py,
                    os.path.join(repo_root, "refresh_unknown_points_after_answer.py"),
                    "--job-id",
                    args.job_id,
                    "--input-root",
                    args.input_root,
                    "--answers-json",
                    args.answers_json,
                    "--input",
                    after_qa_path,
                    "--unknowns",
                    unknowns_path,
                    "--output",
                    unknowns_path,
                ],
                "step_17_refresh_unknowns_after_answer",
            )
            update_job_progress(
                input_root=args.input_root,
                job_id=args.job_id,
                phase=current_phase,
                status="success",
                detail={},
            )
            record_visible_progress(
                log_path=log_path,
                visible_log_path=visible_log_path,
                job_id=args.job_id,
                message="回答内容の反映と不明点の再評価が完了しました",
            )

        current_phase = "step_12_13_question_cycle"
        current_step_label = "Step 12-13: 質問選定/LINE通知"
        update_job_progress(
            input_root=args.input_root,
            job_id=args.job_id,
            phase=current_phase,
            status="running",
            detail={"send_line": bool(args.send_line or _line_push_env_ready())},
        )
        record_visible_progress(
            log_path=log_path,
            visible_log_path=visible_log_path,
            job_id=args.job_id,
            message="不明点からLINE質問を選定しています...",
        )
        qcycle_cmd = [
            py,
            os.path.join(repo_root, "run_question_cycle_once.py"),
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
        if args.send_line or _line_push_env_ready():
            qcycle_cmd.append("--send-line")
        _run_cmd(log_path, qcycle_cmd, "step_12_13_question_cycle")
        update_job_progress(
            input_root=args.input_root,
            job_id=args.job_id,
            phase=current_phase,
            status="success",
            detail={},
        )
        record_visible_progress(
            log_path=log_path,
            visible_log_path=visible_log_path,
            job_id=args.job_id,
            message="質問の選定とLINE通知が完了しました",
        )

        current_phase = "step_11_minutes_generation"
        current_step_label = "Step 11: 議事録生成"
        update_job_progress(
            input_root=args.input_root,
            job_id=args.job_id,
            phase=current_phase,
            status="running",
            detail={},
        )
        update_doc_title_from_hub(hub_meta_path, f"【議事録生成中】{stem}", log_path)
        record_visible_progress(
            log_path=log_path,
            visible_log_path=visible_log_path,
            job_id=args.job_id,
            message="議事録を再生成中...（発言録・議題・決定事項・Next Actionを構成）",
        )
        _run_cmd(
            log_path,
            [
                py,
                os.path.join(repo_root, "generate_minutes_transcript.py"),
                "--job-id",
                args.job_id,
                "--input-root",
                args.input_root,
                "--input",
                after_qa_path,
            ],
            "step_11_generate_minutes_transcript",
        )
        _run_cmd(
            log_path,
            [
                py,
                os.path.join(repo_root, "generate_minutes_other_sections.py"),
                "--job-id",
                args.job_id,
                "--input-root",
                args.input_root,
            ],
            "step_11_generate_minutes_other_sections",
        )
        sync_cmd = [
            py,
            os.path.join(repo_root, "run_docs_hub_e2e.py"),
            "--job-id",
            args.job_id,
            "--input-root",
            args.input_root,
            "--answers-json",
            args.answers_json,
            "--title",
            final_doc_title,
            "--skip-compose",
        ]
        if args.push:
            sync_cmd.append("--push")
        _run_cmd(log_path, sync_cmd, "step_11_sync_docs")
        update_job_progress(
            input_root=args.input_root,
            job_id=args.job_id,
            phase=current_phase,
            status="success",
            detail={},
        )
        record_visible_progress(
            log_path=log_path,
            visible_log_path=visible_log_path,
            job_id=args.job_id,
            message="議事録の再生成とGoogleドキュメント更新が完了しました",
        )

        # Step⑰: ナレッジ蓄積（議事録生成完了後）
        update_job_progress(
            input_root=args.input_root,
            job_id=args.job_id,
            phase="step_17_knowledge_accumulation",
            status="running",
            detail={},
        )
        record_visible_progress(
            log_path=log_path,
            visible_log_path=visible_log_path,
            job_id=args.job_id,
            message="ナレッジの蓄積を確認中...（用語・人名等をスプレッドシートに反映）",
        )
        kr = accumulate_knowledge(job_dir=job_dir, visible_log_path=visible_log_path)
        if not kr.get("enabled"):
            kr_msg = "ナレッジ蓄積: スキップ（KNOWLEDGE_SHEET_IDが未設定のため）"
        elif kr.get("error"):
            kr_msg = f"ナレッジ蓄積でエラーが発生しました: {kr.get('error')}"
        elif kr.get("skipped"):
            kr_msg = f"ナレッジ蓄積: スキップ（{kr.get('reason', '対象なし')}）"
        elif kr.get("updated"):
            before = kr.get('knowledge_count_before', 0)
            after = kr.get('knowledge_count_after', 0)
            kr_msg = f"ナレッジ蓄積が完了しました（{before}件 → {after}件、{after - before}件の新規知識を追加）"
        else:
            kr_msg = f"ナレッジ蓄積: 変更なし（{kr.get('reason', '新規知識なし')}）"
        record_visible_progress(
            log_path=log_path,
            visible_log_path=visible_log_path,
            job_id=args.job_id,
            message=kr_msg,
        )
        update_job_progress(
            input_root=args.input_root,
            job_id=args.job_id,
            phase="step_17_knowledge_accumulation",
            status="success",
            detail=kr,
        )

        current_phase = "done"
        current_step_label = "Step 17: ループ再開完了"
        record_visible_progress(
            log_path=log_path,
            visible_log_path=visible_log_path,
            job_id=args.job_id,
            message="===== 再補正サイクルが完了しました =====",
        )
        finalize_job_progress(input_root=args.input_root, job_id=args.job_id, overall_status="success")
        print(f"job_id={args.job_id}")
        print(f"visible_log={visible_log_path}")
        print("status=success")
    except Exception as e:
        update_job_progress(
            input_root=args.input_root,
            job_id=args.job_id,
            phase=current_phase,
            status="error",
            detail={"step_label": current_step_label, "error": str(e)},
            overall_status="error",
        )
        record_visible_progress(
            log_path=log_path,
            visible_log_path=visible_log_path,
            job_id=args.job_id,
            message=f"{current_step_label}でエラー: {str(e)}",
        )
        raise


if __name__ == "__main__":
    main()
