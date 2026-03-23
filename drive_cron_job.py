print("=== CRON START ===")
import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

from config.settings import settings
from docs.google_docs_writer import run_oauth_docs_create_test_once
from pipeline.cli_runner import run_pipeline_from_cli
from utils.logger import get_logger

logger = get_logger(__name__)

_PROCESSING_RETRY_GRACE_SECONDS = 1800
_CRON_TARGET_AUDIO_EXTENSIONS = (".m4a", ".wav")
_TRANSCRIPTION_DEFERRED_REMAINING = "[TRANSCRIPTION_DEFERRED_REMAINING]"


def _is_transcription_deferred_error(exc: Exception) -> bool:
    return _TRANSCRIPTION_DEFERRED_REMAINING in str(exc)


def _normalize_mm_status(app_props: Dict[str, Any]) -> str:
    raw_status = str((app_props or {}).get("mm_status") or "").strip().lower()
    if raw_status in {"", "pending"}:
        return "pending"
    if raw_status in {"processed", "completed"}:
        return "completed"
    if raw_status in {"processing", "failed"}:
        return raw_status
    return "pending"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_file_name(name: str) -> str:
    invalid = '<>:"/\\|?*'
    safe = "".join("_" if c in invalid else c for c in str(name or "").strip())
    return safe or f"audio_{int(datetime.now(timezone.utc).timestamp())}.m4a"


def _build_drive_read_service() -> Any:
    credentials = settings.get_google_drive_read_credentials()
    return build("drive", "v3", credentials=credentials)


def _build_drive_write_service() -> Any:
    credentials = settings.get_google_drive_write_credentials()
    return build("drive", "v3", credentials=credentials)


def _list_drive_m4a_files_once(drive_service: Any, folder_id: str) -> List[Dict[str, Any]]:
    logger.info(
        "DRIVE_CRON_TARGET_EXTENSIONS: extensions=%s",
        ",".join(_CRON_TARGET_AUDIO_EXTENSIONS),
    )
    query = (
        f"'{folder_id}' in parents and trashed=false "
        "and mimeType!='application/vnd.google-apps.folder'"
    )
    files: List[Dict[str, Any]] = []
    next_page_token = ""

    while True:
        response = (
            drive_service.files()
            .list(
                q=query,
                spaces="drive",
                fields=(
                    "nextPageToken,"
                    "files(id,name,createdTime,modifiedTime,parents,mimeType,appProperties)"
                ),
                orderBy="createdTime",
                pageSize=1000,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                pageToken=next_page_token or None,
            )
            .execute()
        )

        files.extend(response.get("files", []))
        next_page_token = str(response.get("nextPageToken") or "").strip()
        if not next_page_token:
            break

    return [
        f
        for f in files
        if str(f.get("name", "")).lower().endswith(_CRON_TARGET_AUDIO_EXTENSIONS)
    ]


def _is_unprocessed(file_item: Dict[str, Any]) -> bool:
    app_props = file_item.get("appProperties") or {}
    if not isinstance(app_props, dict):
        app_props = {}

    # mm_docs_retry=true なら再実行対象（status に関わらず）
    if str(app_props.get("mm_docs_retry") or "").strip().lower() == "true":
        return True

    status = _normalize_mm_status(app_props)
    if status in {"completed", "failed", "processing"}:
        return False

    return True


def _start_processing_job_if_eligible(
    drive_write_service: Any,
    file_item: Dict[str, Any],
) -> bool:
    file_id = str(file_item.get("id") or "").strip()
    if not file_id:
        return False

    latest = (
        drive_write_service.files()
        .get(
            fileId=file_id,
            fields="appProperties",
            supportsAllDrives=True,
        )
        .execute()
    )
    latest_app_props = latest.get("appProperties") or {}
    if not isinstance(latest_app_props, dict):
        latest_app_props = {}

    latest_status = _normalize_mm_status(latest_app_props)
    is_manual_retry = str(latest_app_props.get("mm_docs_retry") or "").strip().lower() == "true"
    if latest_status in {"processing", "completed", "failed"} and not is_manual_retry:
        logger.info(
            "DRIVE_CRON_SKIP_BY_LATEST_STATUS: file_id=%s latest_status=%s manual_retry=%s",
            file_id,
            latest_status,
            is_manual_retry,
        )
        return False

    processing_props: Dict[str, str] = {
        "mm_status": "processing",
        "processing_started_at": _now_iso(),
    }
    if is_manual_retry:
        processing_props["mm_docs_retry"] = ""

    _set_drive_app_properties(
        drive_service=drive_write_service,
        file_id=file_id,
        properties=processing_props,
    )
    logger.info(
        "DRIVE_CRON_STATUS_TRANSITION: file_id=%s from=%s to=processing manual_retry=%s",
        file_id,
        latest_status,
        is_manual_retry,
    )
    return True


def _download_drive_file(drive_service: Any, file_id: str, destination_path: Path) -> None:
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    request = drive_service.files().get_media(fileId=file_id, supportsAllDrives=True)
    with open(destination_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()


def _download_optional_checkpoint_file(
    drive_service: Any,
    checkpoint_file_id: str,
    destination_path: Path,
) -> bool:
    checkpoint_id = str(checkpoint_file_id or "").strip()
    if not checkpoint_id:
        return False

    try:
        _download_drive_file(
            drive_service=drive_service,
            file_id=checkpoint_id,
            destination_path=destination_path,
        )
        logger.info(
            "DRIVE_CRON_CHECKPOINT_DOWNLOAD_DONE: checkpoint_file_id=%s local_path=%s",
            checkpoint_id,
            destination_path,
        )
        return True
    except Exception as exc:
        logger.warning(
            "DRIVE_CRON_CHECKPOINT_DOWNLOAD_FAILED: checkpoint_file_id=%s reason=%s",
            checkpoint_id,
            exc,
        )
        return False


def _upsert_checkpoint_file(
    drive_service: Any,
    local_checkpoint_path: Path,
    parent_folder_id: str,
    existing_checkpoint_file_id: str,
    checkpoint_file_name: str,
) -> str:
    if not local_checkpoint_path.exists():
        raise FileNotFoundError(f"checkpoint not found: {local_checkpoint_path}")

    media = MediaFileUpload(
        str(local_checkpoint_path),
        mimetype="application/json",
        resumable=False,
    )
    existing_id = str(existing_checkpoint_file_id or "").strip()
    if existing_id:
        response = (
            drive_service.files()
            .update(
                fileId=existing_id,
                media_body=media,
                supportsAllDrives=True,
                fields="id",
            )
            .execute()
        )
        return str(response.get("id") or existing_id)

    body: Dict[str, Any] = {"name": checkpoint_file_name}
    parent_id = str(parent_folder_id or "").strip()
    if parent_id:
        body["parents"] = [parent_id]

    response = (
        drive_service.files()
        .create(
            body=body,
            media_body=media,
            supportsAllDrives=True,
            fields="id",
        )
        .execute()
    )
    return str(response.get("id") or "")


def _delete_drive_file_if_exists(drive_service: Any, file_id: str) -> None:
    target_id = str(file_id or "").strip()
    if not target_id:
        return

    try:
        drive_service.files().delete(fileId=target_id, supportsAllDrives=True).execute()
        logger.info("DRIVE_CRON_CHECKPOINT_DELETE_DONE: checkpoint_file_id=%s", target_id)
    except Exception as exc:
        logger.warning(
            "DRIVE_CRON_CHECKPOINT_DELETE_FAILED: checkpoint_file_id=%s reason=%s",
            target_id,
            exc,
        )


def _read_checkpoint_next_chunk(local_checkpoint_path: Path) -> int:
    if not local_checkpoint_path.exists():
        return 0

    try:
        payload = json.loads(local_checkpoint_path.read_text(encoding="utf-8"))
        return max(0, int(payload.get("next_chunk_index") or 0))
    except Exception:
        return 0


def _set_drive_app_properties(drive_service: Any, file_id: str, properties: Dict[str, str]) -> None:
    existing = (
        drive_service.files()
        .get(
            fileId=file_id,
            fields="appProperties",
            supportsAllDrives=True,
        )
        .execute()
    )
    existing_props = existing.get("appProperties") or {}
    if not isinstance(existing_props, dict):
        existing_props = {}

    merged_props = {**existing_props, **properties}

    drive_service.files().update(
        fileId=file_id,
        body={"appProperties": merged_props},
        supportsAllDrives=True,
    ).execute()


def _move_drive_file_to_folder(
    drive_service: Any,
    file_item: Dict[str, Any],
    target_folder_id: str,
) -> None:
    if not target_folder_id:
        return

    file_id = str(file_item.get("id") or "").strip()
    if not file_id:
        return

    parents = file_item.get("parents") or []
    remove_parents = ",".join([str(p).strip() for p in parents if str(p).strip()])

    drive_service.files().update(
        fileId=file_id,
        addParents=target_folder_id,
        removeParents=remove_parents or None,
        supportsAllDrives=True,
    ).execute()


def _move_audio_file_to_docs_folder(
    drive_service: Any,
    file_item: Dict[str, Any],
    docs_folder_id: str,
) -> None:
    file_id = str(file_item.get("id") or "").strip()
    if not file_id:
        reason = "file_id_empty"
        logger.warning(
            "AUDIO_MOVE_FAILED: file_id=%s folder_id=%s reason=%s",
            file_id,
            docs_folder_id,
            reason,
        )
        raise ValueError(reason)

    target_folder_id = str(docs_folder_id or "").strip()
    if not target_folder_id:
        reason = "docs_folder_id_empty"
        logger.warning(
            "AUDIO_MOVE_FAILED: file_id=%s folder_id=%s reason=%s",
            file_id,
            target_folder_id,
            reason,
        )
        raise ValueError(reason)

    logger.info("AUDIO_MOVE_START: file_id=%s folder_id=%s", file_id, target_folder_id)
    try:
        _move_drive_file_to_folder(
            drive_service=drive_service,
            file_item=file_item,
            target_folder_id=target_folder_id,
        )
        logger.info("AUDIO_MOVE_SUCCESS: file_id=%s folder_id=%s", file_id, target_folder_id)
    except Exception as exc:
        logger.warning(
            "AUDIO_MOVE_FAILED: file_id=%s folder_id=%s reason=%s",
            file_id,
            target_folder_id,
            exc,
        )
        raise


def _record_drive_status(
    drive_service: Any,
    file_item: Dict[str, Any],
    status: str,
    status_backend: str,
    processed_folder_id: str,
    failed_folder_id: str,
    error_message: str = "",
) -> None:
    file_id = str(file_item.get("id") or "").strip()
    if not file_id:
        return

    now = _now_iso()
    props: Dict[str, str] = {"mm_status": status}
    if status == "failed":
        props["failed_at"] = now
        if error_message:
            props["mm_error"] = str(error_message)[:120]

    _set_drive_app_properties(drive_service, file_id=file_id, properties=props)

    if status_backend == "folder_move":
        target_folder_id = processed_folder_id if status == "completed" else failed_folder_id
        _move_drive_file_to_folder(
            drive_service=drive_service,
            file_item=file_item,
            target_folder_id=target_folder_id,
        )


def run_drive_polling_job_once(
    drive_folder_id: str,
    max_files: int,
    status_backend: str,
    processed_folder_id: str,
    failed_folder_id: str,
) -> Dict[str, int]:
    if not drive_folder_id:
        raise ValueError("drive_folder_id is required")

    if status_backend == "folder_move":
        if not processed_folder_id or not failed_folder_id:
            raise ValueError(
                "processed_folder_id and failed_folder_id are required when status_backend=folder_move"
            )

    drive_read_service = _build_drive_read_service()
    drive_write_service = _build_drive_write_service()
    all_files = _list_drive_m4a_files_once(drive_service=drive_read_service, folder_id=drive_folder_id)

    retry_targets = []
    normal_targets = []
    for _item in all_files:
        _app_props = _item.get("appProperties") or {}
        _status = _normalize_mm_status(_app_props)
        _manual_retry = str(_app_props.get("mm_docs_retry") or "").strip().lower() == "true"
        logger.info(
            "DRIVE_CRON_FILTER: file_id=%s file_name=%s mm_status=%s manual_retry=%s app_props=%s",
            _item.get("id", ""),
            _item.get("name", ""),
            _status,
            _manual_retry,
            _app_props,
        )
        if _is_unprocessed(_item):
            if _manual_retry:
                retry_targets.append(_item)
            else:
                normal_targets.append(_item)
        else:
            logger.info(
                "DRIVE_CRON_SKIP: file_id=%s file_name=%s reason=mm_status=%s",
                _item.get("id", ""),
                _item.get("name", ""),
                _status,
            )

    targets = retry_targets + normal_targets
    logger.info(
        "DRIVE_CRON_RETRY_PRIORITY: retry_targets=%s normal_targets=%s",
        len(retry_targets),
        len(normal_targets),
    )

    if max_files > 0:
        targets = targets[:max_files]

    logger.info(
        "DRIVE_CRON_START: folder_id=%s total_m4a=%s target_count=%s max_files=%s backend=%s",
        drive_folder_id,
        len(all_files),
        len(targets),
        max_files,
        status_backend,
    )

    processed_count = 0
    failed_count = 0
    deferred_count = 0

    for item in targets:
        file_id = str(item.get("id") or "").strip()
        file_name = str(item.get("name") or "").strip()
        safe_name = _safe_file_name(file_name)

        with tempfile.TemporaryDirectory(prefix="meeting_audio_") as tmp_dir:
            local_path = Path(tmp_dir) / safe_name
            checkpoint_local_path = Path(tmp_dir) / "whisper_split_checkpoint.json"
            checkpoint_file_id = str(
                ((item.get("appProperties") or {}).get("mm_whisper_checkpoint_file_id") or "")
            ).strip()
            checkpoint_file_name = f".mm_whisper_checkpoint_{file_id}.json"
            parent_folder_id = ""
            _parents = item.get("parents") or []
            if isinstance(_parents, list) and _parents:
                parent_folder_id = str(_parents[0] or "").strip()

            _download_optional_checkpoint_file(
                drive_service=drive_write_service,
                checkpoint_file_id=checkpoint_file_id,
                destination_path=checkpoint_local_path,
            )

            prev_checkpoint_path_env = os.getenv("MM_WHISPER_CHECKPOINT_PATH")
            prev_chunk_limit_env = os.getenv("WHISPER_SPLIT_MAX_CHUNKS_PER_RUN")
            prev_drive_file_id_env = os.getenv("MM_DRIVE_FILE_ID")
            os.environ["MM_WHISPER_CHECKPOINT_PATH"] = str(checkpoint_local_path)
            os.environ["WHISPER_SPLIT_MAX_CHUNKS_PER_RUN"] = "1"  # 1run=1chunkを強制
                logger.info("WHISPER_SPLIT_MAX_CHUNKS_PER_RUN_SET: value=%s", os.environ["WHISPER_SPLIT_MAX_CHUNKS_PER_RUN"])
            os.environ["MM_DRIVE_FILE_ID"] = file_id

            try:
                if not _start_processing_job_if_eligible(
                    drive_write_service=drive_write_service,
                    file_item=item,
                ):
                    logger.info("DRIVE_CRON_SKIP_AFTER_STATUS_RECHECK: file_id=%s", file_id)
                    continue

                logger.info("DRIVE_CRON_DOWNLOAD_START: file_id=%s file_name=%s", file_id, file_name)
                _download_drive_file(
                    drive_service=drive_read_service,
                    file_id=file_id,
                    destination_path=local_path,
                )

                logger.info("DRIVE_CRON_PIPELINE_START: file_id=%s local_path=%s", file_id, local_path)
                pipeline_result = run_pipeline_from_cli(
                    str(local_path.resolve()),
                    auto_selected_audio=False,
                )

                docs_folder_id = str(
                    ((pipeline_result or {}).get("google_doc_result") or {}).get("folder_id") or ""
                ).strip()
                _move_audio_file_to_docs_folder(
                    drive_service=drive_write_service,
                    file_item=item,
                    docs_folder_id=docs_folder_id,
                )

                _set_drive_app_properties(
                    drive_service=drive_write_service,
                    file_id=file_id,
                    properties={
                        "mm_docs_retry": "",
                        "mm_whisper_checkpoint_file_id": "",
                        "mm_whisper_next_chunk": "",
                    },
                )
                _delete_drive_file_if_exists(drive_service=drive_write_service, file_id=checkpoint_file_id)
                _record_drive_status(
                    drive_service=drive_write_service,
                    file_item=item,
                    status="completed",
                    status_backend=status_backend,
                    processed_folder_id=processed_folder_id,
                    failed_folder_id=failed_folder_id,
                )
                processed_count += 1
                logger.info("DRIVE_CRON_PROCESSED: file_id=%s file_name=%s", file_id, file_name)
            except Exception as exc:
                if _is_transcription_deferred_error(exc):
                    deferred_count += 1
                    next_chunk_index = _read_checkpoint_next_chunk(checkpoint_local_path)
                    checkpoint_file_id = _upsert_checkpoint_file(
                        drive_service=drive_write_service,
                        local_checkpoint_path=checkpoint_local_path,
                        parent_folder_id=parent_folder_id,
                        existing_checkpoint_file_id=checkpoint_file_id,
                        checkpoint_file_name=checkpoint_file_name,
                    )
                    logger.info(
                        "DRIVE_CRON_CHECKPOINT_SAVE_DONE: file_id=%s next_chunk_index=%s checkpoint_file_id=%s",
                        file_id,
                        next_chunk_index,
                        checkpoint_file_id,
                    )
                    _set_drive_app_properties(
                        drive_service=drive_write_service,
                        file_id=file_id,
                        properties={
                            "mm_status": "pending",
                            "mm_docs_retry": "true",
                            "mm_whisper_checkpoint_file_id": checkpoint_file_id,
                            "mm_whisper_next_chunk": str(next_chunk_index),
                            "mm_error": "",
                        },
                    )
                    logger.info(
                        "DRIVE_CRON_DEFERRED_FOR_NEXT_RUN: file_id=%s file_name=%s next_chunk_index=%s checkpoint_file_id=%s",
                        file_id,
                        file_name,
                        next_chunk_index,
                        checkpoint_file_id,
                    )
                    continue

                failed_count += 1
                logger.exception("DRIVE_CRON_FAILED: file_id=%s file_name=%s reason=%s", file_id, file_name, exc)
                try:
                    _record_drive_status(
                        drive_service=drive_write_service,
                        file_item=item,
                        status="failed",
                        status_backend=status_backend,
                        processed_folder_id=processed_folder_id,
                        failed_folder_id=failed_folder_id,
                        error_message=str(exc),
                    )
                except Exception as status_exc:
                    logger.exception(
                        "DRIVE_CRON_STATUS_RECORD_FAILED: file_id=%s reason=%s",
                        file_id,
                        status_exc,
                    )
            finally:
                if prev_checkpoint_path_env is None:
                    os.environ.pop("MM_WHISPER_CHECKPOINT_PATH", None)
                else:
                    os.environ["MM_WHISPER_CHECKPOINT_PATH"] = prev_checkpoint_path_env

                if prev_chunk_limit_env is None:
                    os.environ.pop("WHISPER_SPLIT_MAX_CHUNKS_PER_RUN", None)
                else:
                    os.environ["WHISPER_SPLIT_MAX_CHUNKS_PER_RUN"] = prev_chunk_limit_env

                if prev_drive_file_id_env is None:
                    os.environ.pop("MM_DRIVE_FILE_ID", None)
                else:
                    os.environ["MM_DRIVE_FILE_ID"] = prev_drive_file_id_env

    logger.info(
        "DRIVE_CRON_DONE: processed=%s failed=%s deferred=%s scanned=%s",
        processed_count,
        failed_count,
        deferred_count,
        len(all_files),
    )

    return {
        "processed": processed_count,
        "failed": failed_count,
        "deferred": deferred_count,
        "scanned": len(all_files),
        "targets": len(targets),
    }


def main() -> None:
    print("CRON START")
    logger.info("CRON START")
    run_oauth_docs_create_test_once()

    parser = argparse.ArgumentParser(description="Run one-shot Google Drive polling job for Railway Cron")
    parser.add_argument(
        "--drive-folder-id",
        default="",
        help="Google Drive folder ID to scan once",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=1,
        help="Max number of unprocessed files to handle per run (0 means all)",
    )
    parser.add_argument(
        "--status-backend",
        choices=["app_properties", "folder_move"],
        default="app_properties",
        help="How to record processed/failed state on Drive files",
    )
    parser.add_argument(
        "--processed-folder-id",
        default="",
        help="Target folder ID for processed files when status-backend=folder_move",
    )
    parser.add_argument(
        "--failed-folder-id",
        default="",
        help="Target folder ID for failed files when status-backend=folder_move",
    )

    args = parser.parse_args()

    drive_folder_id = str(args.drive_folder_id or "").strip()
    if not drive_folder_id:
        drive_folder_id = str(getattr(settings, "GOOGLE_DRIVE_BASE_FOLDER_ID", "") or "").strip()

    if not drive_folder_id:
        raise ValueError("--drive-folder-id is required")

    run_drive_polling_job_once(
        drive_folder_id=drive_folder_id,
        max_files=max(0, int(args.max_files)),
        status_backend=str(args.status_backend),
        processed_folder_id=str(args.processed_folder_id or "").strip(),
        failed_folder_id=str(args.failed_folder_id or "").strip(),
    )


if __name__ == "__main__":
    main()