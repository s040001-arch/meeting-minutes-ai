import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ──────────────────────────────────────────────
# 定数
# ──────────────────────────────────────────────
SCOPES = ["https://www.googleapis.com/auth/drive"]
SERVICE_ACCOUNT_FILE = "credentials_service_account.json"
WATCH_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "")
STATE_FILE = "data/last_seen_file_ids.json"
LOCK_FILE = "logs/drive_auto_run_once.lock"
LOG_FILE = "logs/drive_auto_run_once.log"

# ★ 変更点①: テキストとオーディオでダウンロード先を分離
INCOMING_AUDIO_DIR = "data/incoming_audio"
INCOMING_TEXT_DIR = "data/incoming_text"           # ← 追加

AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav"}
TEXT_EXTENSIONS = {".txt"}                          # ← 追加
SUPPORTED_EXTENSIONS = AUDIO_EXTENSIONS | TEXT_EXTENSIONS

MIME_TYPE_FOLDER = "mimeType = 'application/vnd.google-apps.folder'"
MIME_TYPE_GOOGLE_DOC = "application/vnd.google-apps.document"

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Google Drive ヘルパー
# ──────────────────────────────────────────────
def get_drive_service():
    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds)


def list_files_in_folder(service, folder_id: str) -> list[dict]:
    """指定フォルダ直下のファイル一覧を取得（フォルダ自体は除外）"""
    results = []
    page_token = None
    query = (
        f"'{folder_id}' in parents"
        f" and mimeType != 'application/vnd.google-apps.folder'"
        f" and trashed = false"
    )
    while True:
        resp = (
            service.files()
            .list(
                q=query,
                fields="nextPageToken, files(id, name, mimeType, parents)",
                pageToken=page_token,
            )
            .execute()
        )
        results.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return results


def create_subfolder(service, parent_id: str, name: str) -> str:
    """parent_id 配下にサブフォルダを作成し、そのIDを返す"""
    body = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(body=body, fields="id").execute()
    return folder["id"]


def move_file_to_folder(service, file_id: str, new_parent_id: str) -> None:
    """ファイルを新しいフォルダに移動"""
    file_meta = (
        service.files().get(fileId=file_id, fields="parents").execute()
    )
    old_parents = ",".join(file_meta.get("parents", []))
    service.files().update(
        fileId=file_id,
        addParents=new_parent_id,
        removeParents=old_parents,
        fields="id, parents",
    ).execute()


def download_file(service, file_id: str, dest_path: str) -> None:
    """Drive からファイルをダウンロード"""
    import io

    request = service.files().get_media(fileId=file_id)
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with open(dest_path, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()


# ★ 変更点②: テキストファイルはダウンロードではなく内容を直接取得して保存
def fetch_text_content(service, file_id: str, dest_path: str) -> None:
    """Drive 上のテキストファイルの内容を API で取得しローカルに保存"""
    import io

    request = service.files().get_media(fileId=file_id)
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    text = buffer.getvalue().decode("utf-8", errors="replace")
    with open(dest_path, "w", encoding="utf-8") as f:
        f.write(text)


def subfolder_has_google_doc(service, subfolder_id: str) -> bool:
    """サブフォルダ内に Google Doc が存在するか"""
    query = (
        f"'{subfolder_id}' in parents"
        f" and mimeType = '{MIME_TYPE_GOOGLE_DOC}'"
        f" and trashed = false"
    )
    resp = (
        service.files()
        .list(q=query, fields="files(id)", pageSize=1)
        .execute()
    )
    return len(resp.get("files", [])) > 0


# ──────────────────────────────────────────────
# 状態管理
# ──────────────────────────────────────────────
def load_seen_ids() -> set[str]:
    if not os.path.isfile(STATE_FILE):
        return set()
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return set(data) if isinstance(data, list) else set()


def save_seen_ids(seen: set[str]) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=2)


# ──────────────────────────────────────────────
# ロック
# ──────────────────────────────────────────────
def acquire_lock() -> bool:
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    if os.path.isfile(LOCK_FILE):
        logger.warning("Lock file exists: %s — skipping run", LOCK_FILE)
        return False
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))
    return True


def release_lock() -> None:
    if os.path.isfile(LOCK_FILE):
        os.remove(LOCK_FILE)


# ──────────────────────────────────────────────
# ★ 変更点③: ファイル種別に応じた取得処理を分離
# ──────────────────────────────────────────────
def resolve_local_path(file_name: str) -> tuple[str, str]:
    """
    ファイル名から (保存先ディレクトリ, ローカルパス) を返す。
    テキストは incoming_text、音声は incoming_audio に振り分ける。
    """
    ext = Path(file_name).suffix.lower()
    if ext in TEXT_EXTENSIONS:
        dest_dir = INCOMING_TEXT_DIR
    else:
        dest_dir = INCOMING_AUDIO_DIR
    local_path = os.path.join(dest_dir, file_name)
    return dest_dir, local_path


def acquire_file_locally(
    service, file_id: str, file_name: str, local_path: str
) -> None:
    """
    ファイル種別に応じてローカルに取得する。
    - テキスト: UTF-8テキストとして保存（バイナリダウンロード不要）
    - 音声: バイナリダウンロード
    """
    ext = Path(file_name).suffix.lower()
    if ext in TEXT_EXTENSIONS:
        logger.info("テキストファイルを直接取得: %s", file_name)
        fetch_text_content(service, file_id, local_path)
    else:
        logger.info("音声ファイルをダウンロード: %s", file_name)
        download_file(service, file_id, local_path)


# ──────────────────────────────────────────────
# ★ 変更点④: 新規ファイル処理を関数に切り出し
# ──────────────────────────────────────────────
def process_new_file(
    service, file_id: str, file_name: str, seen_ids: set[str]
) -> None:
    """新規ファイル1件を処理する"""
    stem = Path(file_name).stem

    # (a) Drive 上にサブフォルダ作成 & ファイル移動
    subfolder_id = create_subfolder(service, WATCH_FOLDER_ID, stem)
    logger.info("サブフォルダ作成: %s (id=%s)", stem, subfolder_id)

    move_file_to_folder(service, file_id, subfolder_id)
    logger.info("ファイル移動完了: %s → %s/", file_name, stem)

    # (b) ローカルに取得（テキスト/音声で処理を分岐）
    _, local_path = resolve_local_path(file_name)
    acquire_file_locally(service, file_id, file_name, local_path)
    logger.info("ローカル取得完了: %s", local_path)

    # (c) run_job_once.py を実行
    job_id = stem
    cmd = [
        sys.executable,
        "run_job_once.py",
        "--job-id",
        job_id,
        "--input-audio",
        local_path,
        "--docs-push",
        "--docs-parent-folder-id",
        WATCH_FOLDER_ID,
        "--docs-subfolder-name",
        stem,
    ]
    logger.info("ジョブ実行開始: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode == 0:
        logger.info("ジョブ成功: job_id=%s", job_id)
    else:
        logger.error(
            "ジョブ失敗: job_id=%s returncode=%d\nstderr=%s",
            job_id,
            result.returncode,
            result.stderr[:500] if result.stderr else "(empty)",
        )

    # (d) 処理済みとして記録
    seen_ids.add(file_id)
    save_seen_ids(seen_ids)


# ──────────────────────────────────────────────
# ★ 変更点⑤: リカバリ処理を関数に切り出し
# ──────────────────────────────────────────────
def recover_incomplete_jobs(service) -> None:
    """
    Drive上のサブフォルダを走査し、Google Docが未生成のものを再処理する
    """
    query = (
        f"'{WATCH_FOLDER_ID}' in parents"
        f" and mimeType = 'application/vnd.google-apps.folder'"
        f" and trashed = false"
    )
    resp = (
        service.files()
        .list(q=query, fields="files(id, name)", pageSize=100)
        .execute()
    )
    subfolders = resp.get("files", [])

    for sf in subfolders:
        sf_id = sf["id"]
        sf_name = sf["name"]

        if subfolder_has_google_doc(service, sf_id):
            continue

        logger.info("未完了ジョブ検出: %s (id=%s)", sf_name, sf_id)

        # サブフォルダ内のファイルを探す
        files_in_sub = list_files_in_folder(service, sf_id)
        target = None
        for f in files_in_sub:
            ext = Path(f["name"]).suffix.lower()
            if ext in SUPPORTED_EXTENSIONS:
                target = f
                break

        if not target:
            logger.warning(
                "リカバリスキップ: 対象ファイルなし folder=%s", sf_name
            )
            continue

        file_name = target["name"]
        _, local_path = resolve_local_path(file_name)

        # ローカルになければ再取得
        if not os.path.isfile(local_path):
            acquire_file_locally(service, target["id"], file_name, local_path)
            logger.info("リカバリ: ファイル再取得 %s", local_path)

        job_id = sf_name
        cmd = [
            sys.executable,
            "run_job_once.py",
            "--job-id",
            job_id,
            "--input-audio",
            local_path,
            "--docs-push",
            "--docs-parent-folder-id",
            WATCH_FOLDER_ID,
            "--docs-subfolder-name",
            sf_name,
        ]
        logger.info("リカバリ実行: %s", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode == 0:
            logger.info("リカバリ成功: job_id=%s", job_id)
        else:
            logger.error(
                "リカバリ失敗: job_id=%s returncode=%d",
                job_id,
                result.returncode,
            )


# ──────────────────────────────────────────────
# メイン
# ──────────────────────────────────────────────
def main() -> None:
    if not WATCH_FOLDER_ID:
        logger.error("環境変数 WATCH_FOLDER_ID が未設定です")
        sys.exit(1)

    if not acquire_lock():
        sys.exit(0)

    try:
        service = get_drive_service()
        seen_ids = load_seen_ids()

        # ── 新規ファイル処理 ──
        all_files = list_files_in_folder(service, WATCH_FOLDER_ID)
        new_files = [
            f
            for f in all_files
            if f["id"] not in seen_ids
            and Path(f["name"]).suffix.lower() in SUPPORTED_EXTENSIONS
        ]

        if not new_files:
            logger.info("新規ファイルなし")
        else:
            logger.info("新規ファイル %d 件検出", len(new_files))

        for f in new_files:
            process_new_file(service, f["id"], f["name"], seen_ids)

        # ── 未完了ジョブのリカバリ ──
        recover_incomplete_jobs(service)

    finally:
        release_lock()


if __name__ == "__main__":
    main()
