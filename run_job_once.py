import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from ai_correct_text import (
    correct_full_text,
    get_last_correct_full_text_meta,
    resolve_openai_api_key,
)
from accumulate_knowledge_step17 import accumulate_knowledge
from detect_unknown_points import detect_unknown_points
from filename_hints import extract_filename_hints, format_hints_for_prompt
from job_context import load_job_context
from progress_tracker import (
    ensure_artifact_flags,
    finalize_job_progress,
    init_job_progress,
    update_job_progress,
)
from repo_env import load_dotenv_local

TEXT_EXTENSIONS = {".txt"}
AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav"}
_APPEND_LOG_INPUT_ROOT = "data/transcriptions"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def log_line(log_path: str, message: str) -> None:
    line = f"[{now_iso()}] {message}"
    print(line)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def append_visible_step_log(visible_log_path: str, message: str) -> None:
    line = f"[{now_iso()}] {message}"
    with open(visible_log_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def record_visible_progress(
    *,
    log_path: str,
    visible_log_path: str,
    job_id: str,
    message: str,
) -> None:
    """
    Railway stdout とローカル簡易ログ、Drive 上の処理ログを同時に更新する。
    ユーザーが「今どこで止まっているか」をステップ単位で見やすくする用途。
    """
    log_line(log_path, f"visible_step: {message}")
    append_visible_step_log(visible_log_path, message)
    append_log_to_drive(job_id, message)


def run_cmd(log_path: str, args: list[str], step: str) -> None:
    cmd_text = " ".join(args)
    log_line(log_path, f"{step}: start cmd={cmd_text}")
    completed = subprocess.run(args, capture_output=True, text=True)
    if completed.stdout.strip():
        log_line(log_path, f"{step}: stdout\n{completed.stdout.rstrip()}")
    if completed.stderr.strip():
        log_line(log_path, f"{step}: stderr\n{completed.stderr.rstrip()}")
        print(f"[run_cmd][{step}] stderr:\n{completed.stderr.rstrip()}", flush=True)
    if completed.returncode != 0:
        print(
            f"[run_cmd][{step}] FAILED rc={completed.returncode} "
            f"stdout_len={len(completed.stdout)} stderr_len={len(completed.stderr)}\n"
            f"STDOUT>>>{completed.stdout[:2000]}<<<\n"
            f"STDERR>>>{completed.stderr[:2000]}<<<",
            flush=True,
        )
        raise RuntimeError(f"{step} failed: exit_code={completed.returncode}")
    log_line(log_path, f"{step}: success")


def run_cmd_with_timeout_retry(
    log_path: str,
    args: list[str],
    step: str,
    timeout_sec: int,
    retry_count: int,
) -> None:
    """
    timeout時のみリトライする実行ヘルパー。
    非timeoutの失敗（exit_code != 0）は即失敗として扱う。
    """
    attempts = retry_count + 1
    for attempt in range(1, attempts + 1):
        cmd_text = " ".join(args)
        log_line(
            log_path,
            f"{step}: start_with_timeout attempt={attempt}/{attempts} timeout_sec={timeout_sec} cmd={cmd_text}",
        )
        try:
            completed = subprocess.run(args, capture_output=True, text=True, timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            log_line(log_path, f"{step}: timeout attempt={attempt}/{attempts} timeout_sec={timeout_sec}")
            if attempt < attempts:
                continue
            raise RuntimeError(
                f"{step} failed: timeout after {attempts} attempts (timeout_sec={timeout_sec})"
            )

        if completed.stdout.strip():
            log_line(log_path, f"{step}: stdout\n{completed.stdout.rstrip()}")
        if completed.stderr.strip():
            log_line(log_path, f"{step}: stderr\n{completed.stderr.rstrip()}")
        if completed.returncode != 0:
            raise RuntimeError(f"{step} failed: exit_code={completed.returncode}")
        log_line(log_path, f"{step}: success attempt={attempt}/{attempts}")
        return


def ensure_after_qa_exists(job_dir: str, log_path: str) -> None:
    after_qa = os.path.join(job_dir, "merged_transcript_after_qa.txt")
    if os.path.isfile(after_qa):
        return
    ai_path = os.path.join(job_dir, "merged_transcript_ai.txt")
    if not os.path.isfile(ai_path):
        raise FileNotFoundError(f"missing ai transcript: {ai_path}")
    shutil.copyfile(ai_path, after_qa)
    log_line(log_path, "fallback: copied merged_transcript_ai.txt -> merged_transcript_after_qa.txt")


def line_push_env_ready() -> bool:
    """LINE Messaging API push に必要な環境変数が揃っているか。"""
    uid = os.getenv("LINE_USER_ID", "").strip()
    tok = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    return bool(uid and tok)


def relocate_input_into_stem_subfolder(input_path: str) -> str:
    """
    入力ファイルのあるディレクトリ直下に、ファイル名（拡張子なし）と同名のフォルダを作り、
    ファイルをその中へ移動する。既に .../<stem>/<stem>.<ext> にある場合は何もしない。
    """
    p = Path(input_path).resolve()
    if not p.is_file():
        return str(p)
    parent = p.parent
    stem = p.stem
    name = p.name
    dest_dir = parent / stem
    dest_file = dest_dir / name
    if p.resolve() == dest_file.resolve():
        return str(p)
    if p.parent.resolve() == dest_dir.resolve():
        return str(p)
    dest_dir.mkdir(parents=True, exist_ok=True)
    if dest_file.exists():
        p.unlink()
        return str(dest_file)
    shutil.move(str(p), str(dest_file))
    return str(dest_file)


def run_transcription_stage_docs_export(
    *,
    log_path: str,
    py: str,
    repo: str,
    job_id: str,
    job_dir: str,
    input_root: str,
    input_audio_path: str,
    merged_path: str,
    docs_chunk_size: int,
    docs_push: bool,
    docs_parent_folder_id: str | None,
    docs_subfolder_name: str | None,
    skip_export_docs: bool,
) -> None:
    """
    merged_transcript.txt 直後（機械補正の前）に、生テキストのみの Google Doc を作成または更新する。
    ユーザーが進捗を確認できるよう、ファイル名は「【文字起こし】<stem>」とする。
    """
    if skip_export_docs or not docs_push or not (docs_parent_folder_id or "").strip():
        log_line(
            log_path,
            "step_4_0_transcription_docs: skipped "
            "(skip_export_docs or not docs_push or missing docs_parent_folder_id)",
        )
        return

    stem = Path(input_audio_path).stem
    input_ext = Path(input_audio_path).suffix.lower()
    if input_ext in TEXT_EXTENSIONS:
        title = f"【処理開始】{stem}"
    else:
        title = f"【文字起こし完了】{stem}"
    hub_meta_path = os.path.join(input_root, job_id, "google_doc_hub.json")
    stage_md = os.path.join(job_dir, "transcript_stage_docs.md")

    with open(merged_path, "r", encoding="utf-8") as f:
        merged_text = f.read()
    with open(stage_md, "w", encoding="utf-8") as f:
        f.write(f"# {title}\n\n## 発言録\n\n{merged_text}")
        if merged_text and not merged_text.endswith("\n"):
            f.write("\n")

    subfolder_name = docs_subfolder_name or stem
    docs_cmd = [
        py,
        os.path.join(repo, "export_minutes_to_google_docs.py"),
        "--job-id",
        job_id,
        "--input-root",
        input_root,
        "--input",
        stage_md,
        "--chunk-size",
        str(docs_chunk_size),
        "--title",
        title,
        "--push",
        "--drive-parent-folder-id",
        docs_parent_folder_id.strip(),
        "--drive-subfolder-name",
        subfolder_name,
        "--write-doc-meta-json",
        hub_meta_path,
    ]
    hub_doc_id = load_google_doc_hub_doc_id(hub_meta_path)
    if hub_doc_id:
        docs_cmd.extend(["--update-doc-id", hub_doc_id])
        log_line(
            log_path,
            "step_4_0_transcription_docs: existing google_doc_hub.json を更新（本文＝文字起こし直後）",
        )

    update_job_progress(
        input_root=input_root,
        job_id=job_id,
        phase="step_4_0_transcription_docs",
        status="running",
        detail={"title": title},
    )
    try:
        run_cmd(log_path, docs_cmd, "step_4_0_transcription_docs")
        update_job_progress(
            input_root=input_root,
            job_id=job_id,
            phase="step_4_0_transcription_docs",
            status="success",
            detail={"title": title},
        )
    except Exception:
        update_job_progress(
            input_root=input_root,
            job_id=job_id,
            phase="step_4_0_transcription_docs",
            status="error",
            detail={"title": title},
        )
        raise


def load_google_doc_hub_doc_id(hub_meta_path: str) -> str | None:
    """run_docs_hub_e2e と同形式の google_doc_hub.json から doc_id を読む（無ければ None）。"""
    if not os.path.isfile(hub_meta_path):
        return None
    try:
        with open(hub_meta_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        s = str(data.get("doc_id") or "").strip()
        return s or None
    except (OSError, json.JSONDecodeError):
        return None


def update_doc_title(doc_id: str, new_title: str, log_path: str) -> None:
    """Google Docs の Drive ファイル名を更新する。"""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token_candidates = ["token_drive.json", "token.json"]
    token_path = next((p for p in token_candidates if os.path.isfile(p)), "")
    if not token_path:
        log_line(log_path, "update_doc_title: skip (no token file)")
        return

    try:
        creds = Credentials.from_authorized_user_file(token_path)
        drive_service = build("drive", "v3", credentials=creds)
        drive_service.files().update(
            fileId=doc_id,
            body={"name": new_title},
            fields="id,name",
        ).execute()
        log_line(log_path, f"update_doc_title: renamed to '{new_title}'")
    except Exception as e:  # noqa: BLE001
        log_line(log_path, f"update_doc_title: skip error={e!r}")


def update_doc_title_from_hub(hub_meta_path: str, new_title: str, log_path: str) -> None:
    doc_id = load_google_doc_hub_doc_id(hub_meta_path)
    if not doc_id:
        log_line(log_path, f"update_doc_title: skip (doc_id not found) title='{new_title}'")
        return
    update_doc_title(doc_id=doc_id, new_title=new_title, log_path=log_path)


def _load_hub_meta(hub_meta_path: str) -> dict:
    try:
        with open(hub_meta_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_hub_meta(hub_meta_path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(hub_meta_path) or ".", exist_ok=True)
    with open(hub_meta_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _build_drive_service() -> object | None:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token_candidates = ["token_drive.json", "token.json"]
    token_path = next((p for p in token_candidates if os.path.isfile(p)), "")
    if not token_path:
        print("ログアップロードスキップ: token なし")
        return None
    try:
        creds = Credentials.from_authorized_user_file(token_path)
        return build("drive", "v3", credentials=creds)
    except Exception as e:  # noqa: BLE001
        print(f"ログアップロードスキップ: Drive service 初期化失敗 error={e!r}")
        return None


def save_hub_folder_ids_from_doc(hub_meta_path: str, log_path: str) -> None:
    """
    Step 4.0 直後に doc_id から parents を辿り、folder_id / subfolder_id を
    google_doc_hub.json へ早期保存する。
    """
    hub_meta = _load_hub_meta(hub_meta_path)
    doc_id = str(hub_meta.get("doc_id") or "").strip()
    if not doc_id:
        log_line(log_path, "save_hub_folder_ids_from_doc: skip (doc_id not found)")
        return

    drive_service = _build_drive_service()
    if drive_service is None:
        log_line(log_path, "save_hub_folder_ids_from_doc: skip (drive service unavailable)")
        return

    try:
        doc_meta = (
            drive_service.files()
            .get(fileId=doc_id, fields="id,parents", supportsAllDrives=True)
            .execute()
        )
        doc_parents = doc_meta.get("parents", [])
        subfolder_id = str(doc_parents[0]).strip() if doc_parents else ""
        folder_id = str(hub_meta.get("folder_id") or "").strip()
        if subfolder_id:
            subfolder_meta = (
                drive_service.files()
                .get(fileId=subfolder_id, fields="id,parents", supportsAllDrives=True)
                .execute()
            )
            subfolder_parents = subfolder_meta.get("parents", [])
            if subfolder_parents:
                folder_id = str(subfolder_parents[0]).strip()
        if not folder_id and not subfolder_id:
            log_line(log_path, "save_hub_folder_ids_from_doc: skip (parents not found)")
            return
        hub_meta["folder_id"] = folder_id
        if subfolder_id:
            hub_meta["subfolder_id"] = subfolder_id
        _write_hub_meta(hub_meta_path, hub_meta)
        log_line(
            log_path,
            f"save_hub_folder_ids_from_doc: saved folder_id={folder_id or '-'} subfolder_id={subfolder_id or '-'}",
        )
    except Exception as e:  # noqa: BLE001
        log_line(log_path, f"save_hub_folder_ids_from_doc: skip error={e!r}")


def append_log_to_drive(job_id: str, message: str) -> None:
    """
    Google Drive 上の処理ログファイルに1行追記する。
    ファイル名: _処理ログ_{job_id}.txt
    アップロード先: google_doc_hub.json の subfolder_id → folder_id の優先順
    """
    from googleapiclient.http import MediaIoBaseDownload, MediaInMemoryUpload
    import io

    try:
        hub_meta_path = os.path.join(_APPEND_LOG_INPUT_ROOT, job_id, "google_doc_hub.json")
        hub_meta = _load_hub_meta(hub_meta_path)
        if not hub_meta:
            print("ログアップロードスキップ: google_doc_hub.json なし")
            return

        folder_id = str(hub_meta.get("subfolder_id") or hub_meta.get("folder_id") or "").strip()
        if not folder_id:
            print("ログアップロードスキップ: folder_id なし")
            return

        drive_service = _build_drive_service()
        if drive_service is None:
            return

        drive_name = f"_処理ログ_{job_id}.txt"
        escaped_name = drive_name.replace("\\", "\\\\").replace("'", "\\'")
        query = f"'{folder_id}' in parents and name='{escaped_name}' and trashed=false"
        result = (
            drive_service.files()
            .list(
                q=query,
                spaces="drive",
                fields="files(id,name)",
                pageSize=10,
                supportsAllDrives=True,
            )
            .execute()
        )
        files = result.get("files", [])

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] {message}"
        existing_text = ""

        if files:
            file_id = str(files[0]["id"])
            request = drive_service.files().get_media(fileId=file_id, supportsAllDrives=True)
            buffer = io.BytesIO()
            downloader = MediaIoBaseDownload(buffer, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            existing_text = buffer.getvalue().decode("utf-8", errors="replace")
            updated_text = existing_text + ("" if not existing_text or existing_text.endswith("\n") else "\n") + line + "\n"
            media = MediaInMemoryUpload(updated_text.encode("utf-8"), mimetype="text/plain", resumable=False)
            drive_service.files().update(
                fileId=file_id,
                media_body=media,
                fields="id,name",
                supportsAllDrives=True,
            ).execute()
            print(f"append_log_to_drive: updated file_id={file_id} message={message}")
        else:
            updated_text = line + "\n"
            media = MediaInMemoryUpload(updated_text.encode("utf-8"), mimetype="text/plain", resumable=False)
            created = (
                drive_service.files()
                .create(
                    body={"name": drive_name, "parents": [folder_id]},
                    media_body=media,
                    fields="id,name",
                    supportsAllDrives=True,
                )
                .execute()
            )
            print(f"append_log_to_drive: created file_id={created.get('id')} message={message}")
    except Exception as e:  # noqa: BLE001
        print(f"append_log_to_drive: failed error={e!r}")


def _load_unknown_points_file(path: str) -> list[dict]:
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def _save_unknown_points_file(path: str, unknown_points: list[dict]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(unknown_points, f, ensure_ascii=False, indent=2)


def merge_unknown_points(
    *,
    ai_unknowns: list[dict],
    regex_unknowns: list[dict],
) -> list[dict]:
    merged: list[dict] = []
    for item in list(ai_unknowns) + list(regex_unknowns):
        if isinstance(item, dict):
            merged.append(item)
    return merged


def restore_known_statuses(
    new_items: list[dict],
    old_items: list[dict],
) -> list[dict]:
    """新しい検出結果に対して、旧リストの回答済み/送信済みステータスを復元する。
    
    再補正サイクルでunknown_points.jsonが上書きされても、
    過去のQ&A回答やaskedステータスを失わないようにする。
    """
    old_by_text: dict[str, dict] = {}
    for item in old_items:
        status = str(item.get("status", "")).strip().lower()
        if status in {"answered", "done", "closed", "resolved", "asked"}:
            text_key = str(item.get("text", "")).strip()
            if text_key:
                old_by_text[text_key] = item

    result: list[dict] = []
    for item in new_items:
        text_key = str(item.get("text", "")).strip()
        old = old_by_text.get(text_key)
        if old:
            item = dict(item)
            item["status"] = old.get("status")
            if old.get("answer"):
                item["answer"] = old["answer"]
            if old.get("answered_by_question_id"):
                item["answered_by_question_id"] = old["answered_by_question_id"]
            if old.get("answered_at"):
                item["answered_at"] = old["answered_at"]
        result.append(item)
    return result


def main() -> None:
    load_dotenv_local()
    parser = argparse.ArgumentParser(
        description="接続優先E2E: 音声1件を最後まで流し、Google Docsまで出力する単発オーケストレータ"
    )
    parser.add_argument("--job-id", required=True, help="対象ジョブID")
    parser.add_argument("--input-audio", required=True, help="入力音声ファイル（m4a/mp3/wav）")
    parser.add_argument(
        "--input-root",
        default="data/transcriptions",
        help="ジョブ出力ルート（デフォルト: data/transcriptions）",
    )
    parser.add_argument("--chunk-seconds", type=int, default=30, help="チャンク秒数（デフォルト: 30）")
    parser.add_argument("--max-chunks", type=int, default=None, help="最大チャンク数（テスト用）")
    parser.add_argument("--whisper-model", default="small", help="Whisperモデル（デフォルト: small）")
    parser.add_argument("--whisper-language", default="ja", help="Whisper言語（デフォルト: ja）")
    parser.add_argument("--compute-type", default="int8", help="Whisper compute type（デフォルト: int8）")
    parser.add_argument("--openai-model", default="gpt-4.1", help="OpenAIモデル（デフォルト: gpt-4.1）")
    parser.add_argument(
        "--min-ai-length-ratio",
        type=float,
        default=0.6,
        help="AI補正結果の最小文字量比率（補正前に対してこれ未満なら採用しない。デフォルト: 0.6）",
    )
    parser.add_argument(
        "--docs-push",
        action="store_true",
        help="指定時はGoogle Docsへpushする（未指定時はdry-run）",
    )
    parser.add_argument(
        "--docs-chunk-size",
        type=int,
        default=5000,
        help="Docs挿入チャンクサイズ（デフォルト: 5000）",
    )
    parser.add_argument(
        "--docs-parent-folder-id",
        default=None,
        help="指定時: 作成したDocsをこのDriveフォルダ配下へ配置する",
    )
    parser.add_argument(
        "--docs-subfolder-name",
        default=None,
        help="指定時: 親フォルダ配下にこのサブフォルダ名を使う",
    )
    parser.add_argument(
        "--step-5-4-timeout-sec",
        type=int,
        default=120,
        help="step_5_4_recorrect_from_line_answer の1回あたりタイムアウト秒（デフォルト: 120）",
    )
    parser.add_argument(
        "--step-5-4-retry-count",
        type=int,
        default=1,
        help="step_5_4_recorrect_from_line_answer の timeout時リトライ回数（デフォルト: 1）",
    )
    parser.add_argument(
        "--no-send-line",
        action="store_true",
        help="質問があっても LINE push しない（検証・オフライン用）",
    )
    parser.add_argument(
        "--skip-export-docs",
        action="store_true",
        help="step_6_3 の Google Docs 出力をスキップ（run_docs_hub_e2e.py 側で Hub 用に一括出力する場合）",
    )
    parser.add_argument(
        "--no-docs-upload-source",
        "--no-docs-upload-source-txt",
        dest="no_docs_upload_source",
        action="store_true",
        help="元の .txt / 音声を Drive の打合せサブフォルダへアップロードしない（後方互換: --no-docs-upload-source-txt）",
    )
    parser.add_argument(
        "--docs-keep-local-source",
        action="store_true",
        help="元ファイルを Drive に上げた後もローカルから削除しない（txt / 音声のソースアップロード時）",
    )
    parser.add_argument(
        "--no-relocate-input-subfolder",
        action="store_true",
        help="入力の親フォルダに「ファイル名（拡張子なし）」サブフォルダを作って移動しない",
    )
    args = parser.parse_args()
    global _APPEND_LOG_INPUT_ROOT
    _APPEND_LOG_INPUT_ROOT = args.input_root

    if not os.path.isfile(args.input_audio):
        raise FileNotFoundError(f"input audio not found: {args.input_audio}")
    if args.max_chunks is not None and args.max_chunks <= 0:
        raise ValueError("--max-chunks must be > 0")
    if args.step_5_4_timeout_sec <= 0:
        raise ValueError("--step-5-4-timeout-sec must be > 0")
    if args.step_5_4_retry_count < 0:
        raise ValueError("--step-5-4-retry-count must be >= 0")

    job_dir = os.path.join(args.input_root, args.job_id)
    os.makedirs(job_dir, exist_ok=True)
    log_path = os.path.join(job_dir, "e2e_run_log.txt")
    visible_log_path = os.path.join(job_dir, "processing_visible_log.txt")
    log_line(log_path, f"job_id={args.job_id}")
    init_job_progress(input_root=args.input_root, job_id=args.job_id)
    current_phase = "start"
    current_step_label = "初期化"
    if not args.no_relocate_input_subfolder:
        before = os.path.abspath(args.input_audio)
        try:
            after = relocate_input_into_stem_subfolder(args.input_audio)
        except RuntimeError as e:
            log_line(log_path, f"input_relocate_subfolder: failed error={e}")
            raise
        if after != before:
            args.input_audio = after
            log_line(
                log_path,
                f"input_relocate_subfolder: moved to {after}",
            )
        else:
            log_line(
                log_path,
                f"input_relocate_subfolder: skip (already in stem folder or unchanged) path={before}",
            )
    else:
        log_line(log_path, "input_relocate_subfolder: skipped (--no-relocate-input-subfolder)")
    log_line(log_path, f"input_audio={args.input_audio}")

    wav_path = os.path.join(job_dir, "input_16k_mono.wav")
    chunks_dir = os.path.join(job_dir, "chunks")
    merged_path = os.path.join(job_dir, "merged_transcript.txt")
    mechanical_path = os.path.join(job_dir, "merged_transcript_mechanical.txt")
    ai_path = os.path.join(job_dir, "merged_transcript_ai.txt")
    unknowns_path = os.path.join(job_dir, "unknown_points.json")
    regex_unknowns_path = os.path.join(job_dir, "unknown_points_regex.json")

    py = sys.executable
    repo = os.getcwd()
    input_ext = Path(args.input_audio).suffix.lower()
    stem = Path(args.input_audio).stem
    filename = Path(args.input_audio).name
    hints = extract_filename_hints(filename)
    hints_prompt_text = format_hints_for_prompt(hints)
    job_context = load_job_context(os.path.join(args.input_root, args.job_id))
    hub_meta_path = os.path.join(args.input_root, args.job_id, "google_doc_hub.json")
    if hints:
        log_line(log_path, f"📎 ファイル名ヒント抽出: {hints}")
        log_line(log_path, f"filename_hints_prompt_enabled={bool(hints_prompt_text)}")
    if job_context:
        log_line(log_path, f"📋 ジョブコンテキスト読み込み: {list(job_context.keys())}")
    try:
        if input_ext in TEXT_EXTENSIONS:
            log_line(log_path, "input_type=txt")
            log_line(log_path, "skipped_steps=2_1_convert_wav,2_2_split_chunks,3_transcribe,4_1_merge_chunks")
            with open(args.input_audio, "r", encoding="utf-8") as f:
                src_text = f.read()
            with open(merged_path, "w", encoding="utf-8") as f:
                f.write(src_text)
            log_line(log_path, f"seed_transcript_path={merged_path} chars={len(src_text)}")
        elif input_ext in AUDIO_EXTENSIONS:
            log_line(log_path, f"input_type=audio ext={input_ext}")
            current_phase = "step_2_1_convert_wav"
            current_step_label = "Step 2.1: WAV変換"
            record_visible_progress(
                log_path=log_path,
                visible_log_path=visible_log_path,
                job_id=args.job_id,
                message="Step 2.1: WAV変換開始",
            )
            run_cmd(
                log_path,
                [py, os.path.join(repo, "ffmpeg_convert_to_wav.py"), "--input", args.input_audio, "--output", wav_path],
                "step_2_1_convert_wav",
            )
            record_visible_progress(
                log_path=log_path,
                visible_log_path=visible_log_path,
                job_id=args.job_id,
                message="Step 2.1: WAV変換完了",
            )
            split_cmd = [
                py,
                os.path.join(repo, "audio_split_chunks.py"),
                "--input",
                wav_path,
                "--output-dir",
                chunks_dir,
                "--chunk-seconds",
                str(args.chunk_seconds),
            ]
            if args.max_chunks is not None:
                split_cmd.extend(["--max-chunks", str(args.max_chunks)])
            current_phase = "step_2_2_split_chunks"
            current_step_label = "Step 2.2: チャンク分割"
            record_visible_progress(
                log_path=log_path,
                visible_log_path=visible_log_path,
                job_id=args.job_id,
                message=f"Step 2.2: チャンク分割開始 chunk_seconds={args.chunk_seconds}",
            )
            run_cmd(log_path, split_cmd, "step_2_2_split_chunks")

            chunk_files = sorted(Path(chunks_dir).glob("chunk_*.wav"))
            if not chunk_files:
                raise RuntimeError(f"no chunk files generated: {chunks_dir}")
            log_line(log_path, f"chunk_count={len(chunk_files)}")
            record_visible_progress(
                log_path=log_path,
                visible_log_path=visible_log_path,
                job_id=args.job_id,
                message=f"Step 2.2: チャンク分割完了 count={len(chunk_files)}",
            )

            current_phase = "step_3_transcribe"
            current_step_label = "Step 3: 文字起こし"
            record_visible_progress(
                log_path=log_path,
                visible_log_path=visible_log_path,
                job_id=args.job_id,
                message=f"Step 3: 文字起こし開始 chunks={len(chunk_files)}",
            )
            for i, chunk_path in enumerate(chunk_files):
                chunk_id = chunk_path.stem
                start_sec = i * args.chunk_seconds
                end_sec = (i + 1) * args.chunk_seconds
                run_cmd(
                    log_path,
                    [
                        py,
                        os.path.join(repo, "transcribe_one_chunk.py"),
                        "--input",
                        str(chunk_path),
                        "--model",
                        args.whisper_model,
                        "--language",
                        args.whisper_language,
                        "--compute-type",
                        args.compute_type,
                        "--job-id",
                        args.job_id,
                        "--chunk-id",
                        chunk_id,
                        "--chunk-index",
                        str(i),
                        "--start-sec",
                        str(start_sec),
                        "--end-sec",
                        str(end_sec),
                        "--output-root",
                        args.input_root,
                    ],
                    f"step_3_transcribe_{chunk_id}",
                )
            record_visible_progress(
                log_path=log_path,
                visible_log_path=visible_log_path,
                job_id=args.job_id,
                message=f"Step 3: 文字起こし完了 chunks={len(chunk_files)}",
            )

            current_phase = "step_4_1_merge_chunks"
            current_step_label = "Step 4.1: 文字起こし結合"
            record_visible_progress(
                log_path=log_path,
                visible_log_path=visible_log_path,
                job_id=args.job_id,
                message="Step 4.1: 文字起こし結合開始",
            )
            run_cmd(
                log_path,
                [py, os.path.join(repo, "transcription_merge_chunks.py"), "--job-id", args.job_id, "--input-root", args.input_root, "--output", merged_path],
                "step_4_1_merge_chunks",
            )
            record_visible_progress(
                log_path=log_path,
                visible_log_path=visible_log_path,
                job_id=args.job_id,
                message="Step 4.1: 文字起こし結合完了",
            )
        else:
            raise ValueError(f"unsupported input extension: {input_ext}")

        run_transcription_stage_docs_export(
            log_path=log_path,
            py=py,
            repo=repo,
            job_id=args.job_id,
            job_dir=job_dir,
            input_root=args.input_root,
            input_audio_path=args.input_audio,
            merged_path=merged_path,
            docs_chunk_size=args.docs_chunk_size,
            docs_push=args.docs_push,
            docs_parent_folder_id=args.docs_parent_folder_id,
            docs_subfolder_name=args.docs_subfolder_name,
            skip_export_docs=args.skip_export_docs,
        )
        save_hub_folder_ids_from_doc(hub_meta_path, log_path)
        record_visible_progress(
            log_path=log_path,
            visible_log_path=visible_log_path,
            job_id=args.job_id,
            message=(
                f"Step 4.0: 処理開始 file={Path(args.input_audio).name} "
                f"mode={'txt' if input_ext in TEXT_EXTENSIONS else 'audio'}"
            ),
        )

        current_phase = "step_4_2_mechanical_correct"
        current_step_label = "Step 4.2: 機械補正"
        update_job_progress(
            input_root=args.input_root,
            job_id=args.job_id,
            phase="step_4_2_mechanical_correct",
            status="running",
            detail={"input_ext": input_ext},
        )
        update_doc_title_from_hub(hub_meta_path, f"【機械補正中】{stem}", log_path)
        record_visible_progress(
            log_path=log_path,
            visible_log_path=visible_log_path,
            job_id=args.job_id,
            message="Step 4.2: 機械補正開始",
        )
        run_cmd(
            log_path,
            [py, os.path.join(repo, "mechanical_correct_text.py"), "--input", merged_path, "--output", mechanical_path],
            "step_4_2_mechanical_correct",
        )
        ensure_artifact_flags(
            input_root=args.input_root,
            job_id=args.job_id,
            artifacts={
                "merged_transcript_mechanical.txt": {
                    "exists": os.path.isfile(mechanical_path),
                }
            },
        )
        update_job_progress(
            input_root=args.input_root,
            job_id=args.job_id,
            phase="step_4_2_mechanical_correct",
            status="success",
            detail={},
        )
        with open(merged_path, "r", encoding="utf-8") as f:
            raw_text = f.read()
        with open(mechanical_path, "r", encoding="utf-8") as f:
            corrected_text = f.read()
        record_visible_progress(
            log_path=log_path,
            visible_log_path=visible_log_path,
            job_id=args.job_id,
            message=f"Step 4.2: 機械補正完了 input={len(raw_text)} → output={len(corrected_text)}",
        )

        # --- Step 4.3: AI correction (full-text, Claude) ---
        log_line(log_path, "step_4_3_ai_correct: starting full_text mode (Claude)")
        with open(mechanical_path, "r", encoding="utf-8") as f:
            mechanical_text = f.read()

        log_line(
            log_path,
            f"step_4_3_ai_correct: input_chars={len(mechanical_text)} model=claude-sonnet-4-20250514",
        )
        update_job_progress(
            input_root=args.input_root,
            job_id=args.job_id,
            phase="step_4_3_ai_correct",
            status="running",
            detail={"chars": len(mechanical_text), "mode": "full_text_claude"},
        )
        current_phase = "step_4_3_ai_correct"
        current_step_label = "Step 4.3: AI補正"
        update_doc_title_from_hub(hub_meta_path, f"【AI補正中】{stem}", log_path)
        record_visible_progress(
            log_path=log_path,
            visible_log_path=visible_log_path,
            job_id=args.job_id,
            message=f"Step 4.3: AI補正開始 input={len(mechanical_text)}",
        )

        PHASE_LABELS = {
            "ai_correct": "AI補正",
        }

        def _on_ai_phase(phase: str):
            label = PHASE_LABELS.get(phase, phase)
            title = f"【AI補正中：{label}】{stem}"
            update_doc_title_from_hub(hub_meta_path, title, log_path)
            append_log_to_drive(args.job_id, f"Step 4.3: {label}開始")

        ai_text = correct_full_text(
            text=mechanical_text,
            on_phase=_on_ai_phase,
            filename_hints=hints,
            visible_log_path=visible_log_path,
            job_context=job_context if job_context else None,
        )

        with open(ai_path, "w", encoding="utf-8") as f:
            f.write(ai_text)

        ratio = len(ai_text) / max(len(mechanical_text), 1)
        log_line(
            log_path,
            f"step_4_3_ai_correct: done input_chars={len(mechanical_text)} "
            f"output_chars={len(ai_text)} ratio={ratio:.3f}",
        )
        update_job_progress(
            input_root=args.input_root,
            job_id=args.job_id,
            phase="step_4_3_ai_correct",
            status="success",
            detail={"chars": len(ai_text), "ratio": round(ratio, 3)},
        )
        correction_meta = get_last_correct_full_text_meta()
        stop_reason = correction_meta.get("stop_reason")
        fallback_reason = correction_meta.get("fallback_reason")
        stop_label = fallback_reason or stop_reason or "unknown"
        record_visible_progress(
            log_path=log_path,
            visible_log_path=visible_log_path,
            job_id=args.job_id,
            message=f"Step 4.3: AI補正完了 output={len(ai_text)} stop_reason={stop_label}",
        )

        # Step 4.35: AI不明点検出（Claude Opus）
        # 既存の回答済み不明点を読み込んで重複質問防止に利用する
        existing_unknowns = _load_unknown_points_file(unknowns_path)
        answered_items = [
            item for item in existing_unknowns
            if str(item.get("status", "")).strip().lower() in {"answered", "done", "closed", "resolved"}
        ]
        update_job_progress(
            input_root=args.input_root,
            job_id=args.job_id,
            phase="step_4_35_ai_unknowns",
            status="running",
            detail={},
        )
        ai_unknown_points = detect_unknown_points(
            ai_text,
            filename_hints=hints,
            job_context=job_context if job_context else None,
            answered_items=answered_items if answered_items else None,
            visible_log_path=visible_log_path,
        )
        _save_unknown_points_file(unknowns_path, ai_unknown_points)
        log_line(
            log_path,
            f"step_4_35_ai_unknowns: saved path={unknowns_path} count={len(ai_unknown_points)}",
        )
        update_job_progress(
            input_root=args.input_root,
            job_id=args.job_id,
            phase="step_4_35_ai_unknowns",
            status="success",
            detail={"ai_unknowns": len(ai_unknown_points)},
        )
        record_visible_progress(
            log_path=log_path,
            visible_log_path=visible_log_path,
            job_id=args.job_id,
            message=f"Step 4.35: AI不明点検出完了 count={len(ai_unknown_points)}",
        )
        update_doc_title_from_hub(hub_meta_path, f"【AI補正完了】{stem}", log_path)

        current_phase = "step_4_4_extract_unknowns"
        current_step_label = "Step 4.4: ハイブリッド不明点検出"
        record_visible_progress(
            log_path=log_path,
            visible_log_path=visible_log_path,
            job_id=args.job_id,
            message="Step 4.4: ハイブリッド不明点検出開始",
        )
        run_cmd(
            log_path,
            [py, os.path.join(repo, "extract_unknown_points.py"), "--input", ai_path, "--output", regex_unknowns_path],
            "step_4_4_extract_unknowns",
        )
        regex_unknown_points = _load_unknown_points_file(regex_unknowns_path)
        merged_unknown_points = merge_unknown_points(
            ai_unknowns=ai_unknown_points,
            regex_unknowns=regex_unknown_points,
        )
        _save_unknown_points_file(unknowns_path, merged_unknown_points)
        log_line(
            log_path,
            "step_4_4_extract_unknowns: merged "
            f"ai_unknowns={len(ai_unknown_points)} regex_unknowns={len(regex_unknown_points)} "
            f"total={len(merged_unknown_points)} saved={unknowns_path}",
        )
        record_visible_progress(
            log_path=log_path,
            visible_log_path=visible_log_path,
            job_id=args.job_id,
            message=f"Step 4.4: ハイブリッド不明点検出完了 total={len(merged_unknown_points)}",
        )
        update_doc_title_from_hub(hub_meta_path, f"【不明点検出完了】{stem}", log_path)
        qcycle_cmd = [
            py,
            os.path.join(repo, "run_question_cycle_once.py"),
            "--job-id",
            args.job_id,
            "--input-root",
            args.input_root,
        ]
        if not args.no_send_line and line_push_env_ready():
            qcycle_cmd.append("--send-line")
            log_line(
                log_path,
                "step_5_question_cycle_prepare: line_push will attempt (LINE_USER_ID + LINE_CHANNEL_ACCESS_TOKEN set)",
            )
        else:
            if args.no_send_line:
                log_line(log_path, "step_5_question_cycle_prepare: line_push disabled (--no-send-line)")
            else:
                log_line(
                    log_path,
                    "step_5_question_cycle_prepare: line_push skipped (set LINE_USER_ID and LINE_CHANNEL_ACCESS_TOKEN to enable)",
                )
        update_job_progress(
            input_root=args.input_root,
            job_id=args.job_id,
            phase="step_5_question_cycle_prepare",
            status="running",
            detail={"send_line": ("--send-line" in qcycle_cmd)},
        )
        current_phase = "step_5_question_cycle_prepare"
        current_step_label = "Step 5: 質問選定"
        record_visible_progress(
            log_path=log_path,
            visible_log_path=visible_log_path,
            job_id=args.job_id,
            message="Step 5: 質問選定開始",
        )
        run_cmd(log_path, qcycle_cmd, "step_5_question_cycle_prepare")
        update_job_progress(
            input_root=args.input_root,
            job_id=args.job_id,
            phase="step_5_question_cycle_prepare",
            status="success",
            detail={"question_sent": bool(qcycle_cmd and "--send-line" in qcycle_cmd)},
        )
        record_visible_progress(
            log_path=log_path,
            visible_log_path=visible_log_path,
            job_id=args.job_id,
            message="Step 5: 質問選定完了",
        )

        line_answers = os.path.join("data", "line_answers.json")
        if os.path.isfile(line_answers):
            try:
                with open(line_answers, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list) and len(data) > 0:
                    current_phase = "step_5_4_recorrect_from_line_answer"
                    current_step_label = "Step 5.4: 回答反映"
                    record_visible_progress(
                        log_path=log_path,
                        visible_log_path=visible_log_path,
                        job_id=args.job_id,
                        message="Step 5.4: 回答反映開始",
                    )
                    run_cmd_with_timeout_retry(
                        log_path,
                        [py, os.path.join(repo, "recorrect_from_line_answer.py"), "--job-id", args.job_id],
                        "step_5_4_recorrect_from_line_answer",
                        timeout_sec=args.step_5_4_timeout_sec,
                        retry_count=args.step_5_4_retry_count,
                    )
                    record_visible_progress(
                        log_path=log_path,
                        visible_log_path=visible_log_path,
                        job_id=args.job_id,
                        message="Step 5.4: 回答反映完了",
                    )
                else:
                    ensure_after_qa_exists(job_dir, log_path)
            except Exception as e:
                log_line(
                    log_path,
                    (
                        "step_5_4_recorrect_from_line_answer: skipped "
                        f"error={e} fallback=merged_transcript_ai_to_after_qa"
                    ),
                )
                ensure_after_qa_exists(job_dir, log_path)
        else:
            ensure_after_qa_exists(job_dir, log_path)

        update_job_progress(
            input_root=args.input_root,
            job_id=args.job_id,
            phase="step_6_1_generate_minutes_transcript",
            status="running",
            detail={},
        )
        current_phase = "step_6_1_generate_minutes_transcript"
        current_step_label = "Step 6.1: 議事録生成"
        update_doc_title_from_hub(hub_meta_path, f"【議事録生成中】{stem}", log_path)
        record_visible_progress(
            log_path=log_path,
            visible_log_path=visible_log_path,
            job_id=args.job_id,
            message="Step 6.1: 議事録生成開始",
        )
        run_cmd(
            log_path,
            [py, os.path.join(repo, "generate_minutes_transcript.py"), "--job-id", args.job_id, "--input-root", args.input_root],
            "step_6_1_generate_minutes_transcript",
        )
        update_job_progress(
            input_root=args.input_root,
            job_id=args.job_id,
            phase="step_6_1_generate_minutes_transcript",
            status="success",
            detail={},
        )
        record_visible_progress(
            log_path=log_path,
            visible_log_path=visible_log_path,
            job_id=args.job_id,
            message="Step 6.1: 議事録生成完了",
        )
        update_job_progress(
            input_root=args.input_root,
            job_id=args.job_id,
            phase="step_6_2_generate_other_sections",
            status="running",
            detail={},
        )
        current_phase = "step_6_2_generate_other_sections"
        current_step_label = "Step 6.2: 議事録整形"
        record_visible_progress(
            log_path=log_path,
            visible_log_path=visible_log_path,
            job_id=args.job_id,
            message="Step 6.2: 議事録整形開始",
        )
        run_cmd(
            log_path,
            [py, os.path.join(repo, "generate_minutes_other_sections.py"), "--job-id", args.job_id, "--input-root", args.input_root],
            "step_6_2_generate_other_sections",
        )
        update_job_progress(
            input_root=args.input_root,
            job_id=args.job_id,
            phase="step_6_2_generate_other_sections",
            status="success",
            detail={},
        )
        record_visible_progress(
            log_path=log_path,
            visible_log_path=visible_log_path,
            job_id=args.job_id,
            message="Step 6.2: 議事録整形完了",
        )

        if args.skip_export_docs:
            current_phase = "step_6_3_export_docs"
            current_step_label = "Step 6.3: Googleドキュメント出力"
            log_line(log_path, "step_6_3_export_docs: skipped (--skip-export-docs)")
            update_job_progress(
                input_root=args.input_root,
                job_id=args.job_id,
                phase="step_6_3_export_docs",
                status="skipped",
                detail={"reason": "flag --skip-export-docs"},
            )
            record_visible_progress(
                log_path=log_path,
                visible_log_path=visible_log_path,
                job_id=args.job_id,
                message="Step 6.3: Googleドキュメント出力スキップ",
            )
        else:
            update_job_progress(
                input_root=args.input_root,
                job_id=args.job_id,
                phase="step_6_3_export_docs",
                status="running",
                detail={"docs_push": bool(args.docs_push)},
            )
            current_phase = "step_6_3_export_docs"
            current_step_label = "Step 6.3: Googleドキュメント出力"
            record_visible_progress(
                log_path=log_path,
                visible_log_path=visible_log_path,
                job_id=args.job_id,
                message="Step 6.3: Googleドキュメント出力開始",
            )
            docs_cmd = [
                py,
                os.path.join(repo, "export_minutes_to_google_docs.py"),
                "--job-id",
                args.job_id,
                "--chunk-size",
                str(args.docs_chunk_size),
                "--title",
                Path(args.input_audio).stem,
            ]
            if args.docs_parent_folder_id:
                docs_cmd.extend(["--drive-parent-folder-id", args.docs_parent_folder_id])
                subfolder_name = args.docs_subfolder_name
                if not subfolder_name:
                    subfolder_name = Path(args.input_audio).stem
                docs_cmd.extend(["--drive-subfolder-name", subfolder_name])
            if args.docs_push:
                docs_cmd.append("--push")
                hub_doc_id = load_google_doc_hub_doc_id(hub_meta_path)
                if hub_doc_id:
                    docs_cmd.extend(["--update-doc-id", hub_doc_id])
                    log_line(
                        log_path,
                        "step_6_3_export_docs: google_doc_hub.json より既存 doc_id を再利用（本文更新）",
                    )
                docs_cmd.extend(["--write-doc-meta-json", hub_meta_path])
            if (
                args.docs_push
                and args.docs_parent_folder_id
                and (input_ext in TEXT_EXTENSIONS or input_ext in AUDIO_EXTENSIONS)
                and not args.no_docs_upload_source
            ):
                docs_cmd.extend(["--upload-local-file", os.path.abspath(args.input_audio)])
                if not args.docs_keep_local_source:
                    docs_cmd.append("--delete-local-after-upload")
            run_cmd(log_path, docs_cmd, "step_6_3_export_docs")
            update_job_progress(
                input_root=args.input_root,
                job_id=args.job_id,
                phase="step_6_3_export_docs",
                status="success",
                detail={"docs_cmd": " ".join(docs_cmd[:6]) + " ..."},
            )
            record_visible_progress(
                log_path=log_path,
                visible_log_path=visible_log_path,
                job_id=args.job_id,
                message="Step 6.3: Googleドキュメント出力完了",
            )

        # Step⑰: ナレッジ蓄積（議事録生成完了後）
        update_job_progress(
            input_root=args.input_root,
            job_id=args.job_id,
            phase="step_17_knowledge_accumulation",
            status="running",
            detail={},
        )
        accumulate_knowledge(job_dir=job_dir, visible_log_path=visible_log_path)
        update_job_progress(
            input_root=args.input_root,
            job_id=args.job_id,
            phase="step_17_knowledge_accumulation",
            status="success",
            detail={},
        )

        log_line(log_path, "pipeline_status=success")
        current_phase = "done"
        current_step_label = "処理完了"
        record_visible_progress(
            log_path=log_path,
            visible_log_path=visible_log_path,
            job_id=args.job_id,
            message="処理完了",
        )
        print(f"job_id={args.job_id}")
        print(f"log={log_path}")
        print(f"visible_log={visible_log_path}")
        print("status=success")
        finalize_job_progress(input_root=args.input_root, job_id=args.job_id, overall_status="success")
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
