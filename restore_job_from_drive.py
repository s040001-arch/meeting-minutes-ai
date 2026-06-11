"""Railway 再デプロイ等で失われたジョブを Google Drive サブフォルダから復元する。"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from google.oauth2 import service_account
from googleapiclient.discovery import build

from drive_auto_run_once import (
    SUPPORTED_EXTENSIONS,
    _build_credentials,
    download_drive_file,
    ensure_extension_by_mime,
)
from google_drive_poll_new_files import list_files_in_folder
from meeting_profile import ensure_meeting_profile, strip_status_prefix
from mechanical_correct_text import apply_mechanical_corrections
from repo_env import load_dotenv_local

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
DOCS_MIME = "application/vnd.google-apps.document"


def _py() -> str:
    return sys.executable


def _find_subfolder(service, root_folder_id: str, contains: str) -> dict:
    q = (
        f"'{root_folder_id}' in parents and "
        "mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    folders = (
        service.files()
        .list(q=q, spaces="drive", fields="files(id,name,createdTime)", pageSize=200)
        .execute()
        .get("files", [])
    )
    matches = [f for f in folders if contains in str(f.get("name") or "")]
    if not matches:
        raise FileNotFoundError(
            f"no Drive subfolder containing {contains!r} under folder_id={root_folder_id}"
        )
    matches.sort(key=lambda x: str(x.get("createdTime") or ""), reverse=True)
    return matches[0]


def _pick_source_file(children: list[dict]) -> dict:
    for child in children:
        mime = str(child.get("mimeType") or "")
        ext = os.path.splitext(str(child.get("name") or ""))[1].lower()
        if mime == DOCS_MIME:
            continue
        if ext in SUPPORTED_EXTENSIONS or mime == "text/plain" or mime.startswith("audio/"):
            return child
    raise FileNotFoundError("source txt/audio not found in Drive subfolder")


def _pick_google_doc(children: list[dict]) -> dict | None:
    docs = [c for c in children if str(c.get("mimeType") or "") == DOCS_MIME]
    if not docs:
        return None
    docs.sort(key=lambda x: str(x.get("modifiedTime") or x.get("name") or ""), reverse=True)
    return docs[0]


def _write_google_doc_hub(
    path: str,
    *,
    job_id: str,
    doc_id: str,
    title: str,
    folder_id: str,
    subfolder_id: str,
) -> None:
    payload = {
        "job_id": job_id,
        "doc_id": doc_id,
        "doc_url": f"https://docs.google.com/document/d/{doc_id}/edit",
        "title": title,
        "mode": "update",
        "folder_id": folder_id,
        "subfolder_id": subfolder_id,
    }
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _rebuild_ai_text(job_dir: str, job_id: str, merged_path: str) -> str:
    mechanical_path = os.path.join(job_dir, "merged_transcript_mechanical.txt")
    ai_path = os.path.join(job_dir, "merged_transcript_ai.txt")

    with open(merged_path, "r", encoding="utf-8") as f:
        merged = f.read()
    mechanical = apply_mechanical_corrections(merged)
    with open(mechanical_path, "w", encoding="utf-8") as f:
        f.write(mechanical)

    from ai_correct_text import correct_full_text, get_last_correct_full_text_meta

    corrected = correct_full_text(text=mechanical)
    meta = get_last_correct_full_text_meta()
    with open(ai_path, "w", encoding="utf-8") as f:
        f.write(corrected)
    meta_path = os.path.join(job_dir, "correction_meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(
        f"rebuild_ai_done mechanical_chars={len(mechanical)} ai_chars={len(corrected)}",
        flush=True,
    )
    return ai_path


def main() -> None:
    load_dotenv_local()
    parser = argparse.ArgumentParser(
        description="Drive サブフォルダからジョブ成果物を /app/data/transcriptions に復元"
    )
    parser.add_argument("--job-id", required=True)
    parser.add_argument(
        "--subfolder-contains",
        required=True,
        help="Drive 案件サブフォルダ名に含まれる文字列（例: 2026_0610_ヒロセ）",
    )
    parser.add_argument(
        "--folder-id",
        default=None,
        help="監視ルート Drive フォルダ ID（未指定時 DRIVE_FOLDER_ID）",
    )
    parser.add_argument("--input-root", default="data/transcriptions")
    parser.add_argument(
        "--rebuild-ai",
        action="store_true",
        help="merged_transcript_ai.txt を Step 7+8 から再生成する",
    )
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="merged_transcript.txt が既にある場合は Drive ダウンロードを省略",
    )
    args = parser.parse_args()

    folder_id = (args.folder_id or os.getenv("DRIVE_FOLDER_ID", "").strip())
    if not folder_id:
        raise SystemExit("DRIVE_FOLDER_ID が未設定です")

    job_dir = os.path.join(args.input_root, args.job_id)
    os.makedirs(job_dir, exist_ok=True)
    merged_path = os.path.join(job_dir, "merged_transcript.txt")
    hub_path = os.path.join(job_dir, "google_doc_hub.json")

    creds = _build_credentials()
    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    subfolder = _find_subfolder(service, folder_id, args.subfolder_contains)
    subfolder_id = str(subfolder["id"])
    subfolder_name = str(subfolder.get("name") or "")
    children = list_files_in_folder(service, subfolder_id)
    source = _pick_source_file(children)
    source_name = ensure_extension_by_mime(
        str(source.get("name") or "source.txt"),
        str(source.get("mimeType") or ""),
    )

    if not args.skip_download or not os.path.isfile(merged_path):
        download_path = os.path.join(job_dir, os.path.basename(source_name))
        download_drive_file(service, str(source["id"]), download_path)
        if download_path != merged_path:
            with open(download_path, "rb") as src, open(merged_path, "wb") as dst:
                dst.write(src.read())
        print(f"downloaded_source={merged_path}", flush=True)

    doc = _pick_google_doc(children)
    if doc and not os.path.isfile(hub_path):
        doc_title = strip_status_prefix(str(doc.get("name") or subfolder_name))
        _write_google_doc_hub(
            hub_path,
            job_id=args.job_id,
            doc_id=str(doc["id"]),
            title=doc_title,
            folder_id=folder_id,
            subfolder_id=subfolder_id,
        )
        print(f"google_doc_hub_written doc_id={doc['id']}", flush=True)

    ensure_meeting_profile(job_dir, job_id=args.job_id, filename=source_name)

    ai_path = os.path.join(job_dir, "merged_transcript_ai.txt")
    if args.rebuild_ai or not os.path.isfile(ai_path):
        _rebuild_ai_text(job_dir, args.job_id, merged_path)

    after_qa = os.path.join(job_dir, "merged_transcript_after_qa.txt")
    if not os.path.isfile(after_qa) and os.path.isfile(ai_path):
        with open(ai_path, "r", encoding="utf-8") as src, open(after_qa, "w", encoding="utf-8") as dst:
            dst.write(src.read())

    print(f"restore_job_from_drive_done job_id={args.job_id} subfolder={subfolder_name}")


if __name__ == "__main__":
    main()
