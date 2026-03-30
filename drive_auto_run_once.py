import argparse
import os
import re
import subprocess
import sys
from datetime import datetime
from typing import Any, Dict, List

from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from google_drive_poll_new_files import (
    list_files_in_folder,
    load_last_seen_ids,
    save_last_seen_ids,
)
from repo_env import load_dotenv_local

SUPPORTED_EXTENSIONS = {".m4a", ".mp3", ".wav", ".txt"}
DOCS_EDITOR_PREFIX = "application/vnd.google-apps."
DRIVE_AUTO_SCOPES = ["https://www.googleapis.com/auth/drive"]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_drive_credentials(credentials_json_path: str, token_json_path: str) -> Credentials:
    creds: Credentials | None = None
    if os.path.exists(token_json_path):
        creds = Credentials.from_authorized_user_file(token_json_path, DRIVE_AUTO_SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            return creds
        except RefreshError:
            creds = None

    flow = InstalledAppFlow.from_client_secrets_file(credentials_json_path, DRIVE_AUTO_SCOPES)
    creds = flow.run_local_server(port=0)
    os.makedirs(os.path.dirname(token_json_path) or ".", exist_ok=True)
    with open(token_json_path, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    return creds


def log_line(log_path: str, message: str) -> None:
    line = f"[{now_iso()}] {message}"
    print(line)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def acquire_lock(lock_path: str) -> bool:
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"pid={os.getpid()}\n")
        f.write(f"started_at={now_iso()}\n")
    return True


def release_lock(lock_path: str) -> None:
    try:
        os.remove(lock_path)
    except FileNotFoundError:
        pass


def sanitize_name_for_job(raw: str) -> str:
    s = re.sub(r"\W+", "_", raw, flags=re.UNICODE).strip("_")
    return s[:40] if s else "audio"


def build_job_id(file_name: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = os.path.splitext(file_name)[0]
    return f"job_{stamp}_{sanitize_name_for_job(stem)}"


def select_one_new_file(files: List[Dict[str, Any]], known_ids: set[str]) -> Dict[str, Any] | None:
    candidates: List[Dict[str, Any]] = []
    for f in files:
        file_id = str(f.get("id") or "")
        if not file_id or file_id in known_ids:
            continue
        mime_type = str(f.get("mimeType") or "")
        if mime_type == "application/vnd.google-apps.folder":
            continue
        if mime_type.startswith(DOCS_EDITOR_PREFIX):
            continue
        ext = os.path.splitext(str(f.get("name") or ""))[1].lower()
        # Drive上で拡張子が付かない text/plain があるため、mimeType でも判定する
        is_supported_ext = ext in SUPPORTED_EXTENSIONS
        is_supported_mime = mime_type == "text/plain" or mime_type.startswith("audio/")
        if not (is_supported_ext or is_supported_mime):
            continue
        candidates.append(f)
    if not candidates:
        return None
    # modifiedTime -> name の順で最古を先に処理（FIFO寄り）
    candidates.sort(key=lambda x: (str(x.get("modifiedTime", "")), str(x.get("name", ""))))
    return candidates[0]


def download_drive_file(service, file_id: str, output_path: str) -> None:
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    request = service.files().get_media(fileId=file_id)
    with open(output_path, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()


def ensure_extension_by_mime(file_name: str, mime_type: str) -> str:
    ext = os.path.splitext(file_name)[1].lower()
    if ext:
        return file_name
    if mime_type == "text/plain":
        return f"{file_name}.txt"
    if mime_type in {"audio/mpeg"}:
        return f"{file_name}.mp3"
    if mime_type in {"audio/wav", "audio/x-wav"}:
        return f"{file_name}.wav"
    if mime_type in {"audio/mp4", "audio/x-m4a"}:
        return f"{file_name}.m4a"
    return file_name


def ensure_drive_subfolder(service, parent_folder_id: str, subfolder_name: str) -> str:
    safe_name = subfolder_name.strip()
    if not safe_name:
        raise ValueError("subfolder name is empty.")
    escaped = safe_name.replace("\\", "\\\\").replace("'", "\\'")
    query = (
        f"'{parent_folder_id}' in parents and "
        "mimeType='application/vnd.google-apps.folder' and "
        f"name='{escaped}' and trashed=false"
    )
    result = (
        service.files()
        .list(q=query, spaces="drive", fields="files(id,name)", pageSize=10)
        .execute()
    )
    files = result.get("files", [])
    if files:
        return str(files[0]["id"])
    created = (
        service.files()
        .create(
            body={
                "name": safe_name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_folder_id],
            },
            fields="id,name",
        )
        .execute()
    )
    return str(created["id"])


def move_drive_file_to_folder(service, file_id: str, folder_id: str) -> None:
    file_meta = service.files().get(fileId=file_id, fields="id,parents").execute()
    prev_parents = ",".join(file_meta.get("parents", []))
    service.files().update(
        fileId=file_id,
        addParents=folder_id,
        removeParents=prev_parents,
        fields="id,parents",
    ).execute()


def run_pipeline(
    repo_root: str,
    job_id: str,
    input_audio_path: str,
    docs_chunk_size: int,
    max_chunks: int | None,
    log_path: str,
    docs_parent_folder_id: str | None = None,
    docs_subfolder_name: str | None = None,
) -> None:
    cmd = [
        sys.executable,
        os.path.join(repo_root, "run_job_once.py"),
        "--job-id",
        job_id,
        "--input-audio",
        input_audio_path,
        "--docs-push",
        "--docs-chunk-size",
        str(docs_chunk_size),
    ]
    if docs_parent_folder_id:
        cmd.extend(["--docs-parent-folder-id", docs_parent_folder_id])
    if docs_subfolder_name:
        cmd.extend(["--docs-subfolder-name", docs_subfolder_name])
    # Drive 上の元ファイルは処理後に move で案件サブフォルダへ移すため、
    # ここでローカルから同内容を再アップロードしない（手動 run_job_once は従来どおり）。
    cmd.append("--no-docs-upload-source")
    if max_chunks is not None:
        cmd.extend(["--max-chunks", str(max_chunks)])

    log_line(log_path, f"run_job_once_start cmd={' '.join(cmd)}")
    completed = subprocess.run(cmd, capture_output=True, text=True)
    if completed.stdout.strip():
        log_line(log_path, f"run_job_once_stdout\n{completed.stdout.rstrip()}")
    if completed.stderr.strip():
        log_line(log_path, f"run_job_once_stderr\n{completed.stderr.rstrip()}")
    if completed.returncode != 0:
        raise RuntimeError(f"run_job_once failed: exit_code={completed.returncode}")
    log_line(log_path, "run_job_once_success")


def main() -> None:
    load_dotenv_local()
    parser = argparse.ArgumentParser(
        description=(
            "監視フォルダ直下の新規ファイル1件を検知し、Drive上でstem名サブフォルダへ移動後に "
            "ダウンロードして run_job_once.py を起動する"
        )
    )
    parser.add_argument("--credentials", default="credentials.json", help="OAuthクライアントJSON")
    parser.add_argument(
        "--token",
        default="token_drive.json",
        help="Drive用OAuthトークン保存先/読込先（デフォルト: token_drive.json）",
    )
    parser.add_argument(
        "--folder-id",
        default=None,
        help="監視対象のGoogle DriveフォルダID（未指定時は環境変数 DRIVE_FOLDER_ID）",
    )
    parser.add_argument(
        "--state",
        default="data/last_seen_file_ids.json",
        help="既知file_id保存先（デフォルト: data/last_seen_file_ids.json）",
    )
    parser.add_argument(
        "--download-dir",
        default=os.path.join("data", "incoming_audio"),
        help="Driveファイルのダウンロード先",
    )
    parser.add_argument(
        "--docs-chunk-size",
        type=int,
        default=5000,
        help="run_job_once.py へ渡す Docs 分割サイズ",
    )
    parser.add_argument(
        "--max-chunks",
        type=int,
        default=None,
        help="run_job_once.py へ渡す max-chunks（検証用）",
    )
    parser.add_argument(
        "--update-state",
        action="store_true",
        help="検知後に state を更新する（同一ファイル再処理防止）",
    )
    parser.add_argument(
        "--lock-file",
        default=os.path.join("logs", "drive_auto_run_once.lock"),
        help="二重実行防止ロックファイル（デフォルト: logs/drive_auto_run_once.lock）",
    )
    args = parser.parse_args()

    folder_id = (args.folder_id or os.getenv("DRIVE_FOLDER_ID", "").strip())
    if not folder_id:
        raise SystemExit(
            "監視フォルダが未指定です。--folder-id または環境変数 DRIVE_FOLDER_ID を設定してください。"
        )
    args.folder_id = folder_id

    repo_root = os.getcwd()
    os.makedirs("logs", exist_ok=True)
    log_path = os.path.join("logs", "drive_auto_run_once.log")
    log_line(log_path, "drive_auto_run_once_start")
    if not acquire_lock(args.lock_file):
        log_line(log_path, f"skip_locked lock_file={args.lock_file}")
        print("status=skipped_locked")
        print(f"lock_file={args.lock_file}")
        return

    try:
        known_ids = load_last_seen_ids(args.state)
        creds = load_drive_credentials(args.credentials, args.token)
        service = build("drive", "v3", credentials=creds)

        try:
            files = list_files_in_folder(service, args.folder_id)
        except HttpError as e:
            raise RuntimeError(f"Drive API error: {e}") from e

        selected = select_one_new_file(files, known_ids)
        if not selected:
            log_line(log_path, "no_new_files")
            print("status=no_new_files")
            return

        file_id = str(selected.get("id"))
        file_name = str(selected.get("name") or f"{file_id}.bin")
        mime_type = str(selected.get("mimeType") or "")
        file_name = ensure_extension_by_mime(file_name, mime_type)
        drive_subfolder_name = os.path.splitext(file_name)[0]
        target_folder_id = ensure_drive_subfolder(
            service=service,
            parent_folder_id=args.folder_id,
            subfolder_name=drive_subfolder_name,
        )
        # Drive 上で先に stem 名フォルダへ移動してから取得・処理する（監視直下のみ新規検知）。
        move_drive_file_to_folder(service, file_id=file_id, folder_id=target_folder_id)
        log_line(
            log_path,
            f"source_file_moved_to_stem_folder file_id={file_id} target_folder_id={target_folder_id}",
        )

        safe_name = re.sub(r"[\\/:*?\"<>|]", "_", file_name)
        downloaded_path = os.path.join(args.download_dir, safe_name)
        log_line(log_path, f"selected_file id={file_id} name={file_name}")
        download_drive_file(service, file_id, downloaded_path)
        log_line(log_path, f"downloaded_to={downloaded_path}")

        job_id = build_job_id(file_name)
        run_pipeline(
            repo_root=repo_root,
            job_id=job_id,
            input_audio_path=downloaded_path,
            docs_chunk_size=args.docs_chunk_size,
            max_chunks=args.max_chunks,
            log_path=log_path,
            docs_parent_folder_id=args.folder_id,
            docs_subfolder_name=drive_subfolder_name,
        )

        if args.update_state:
            current_ids = {str(f.get("id")) for f in files if f.get("id")}
            save_last_seen_ids(args.state, current_ids)
            log_line(log_path, f"updated_state={args.state}")

        print(f"status=success")
        print(f"job_id={job_id}")
        print(f"processed_file_id={file_id}")
        print(f"processed_file_name={file_name}")
        print(f"target_folder_id={target_folder_id}")
        print(f"log={log_path}")
    finally:
        release_lock(args.lock_file)


if __name__ == "__main__":
    main()
