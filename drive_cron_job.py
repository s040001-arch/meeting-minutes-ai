print("=== CRON START ===")
import argparse
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from config.settings import settings
from pipeline.cli_runner import run_pipeline_from_cli
from utils.logger import get_logger

logger = get_logger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_file_name(name: str) -> str:
    invalid = '<>:"/\\|?*'
    safe = "".join("_" if c in invalid else c for c in str(name or "").strip())
    return safe or f"audio_{int(datetime.now(timezone.utc).timestamp())}.m4a"


def _build_drive_service() -> Any:
    credentials = settings.get_google_credentials()
    return build("drive", "v3", credentials=credentials)


def _list_drive_m4a_files_once(drive_service: Any, folder_id: str) -> List[Dict[str, Any]]:
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

    return [f for f in files if str(f.get("name", "")).lower().endswith(".m4a")]


def _is_unprocessed(file_item: Dict[str, Any]) -> bool:
    app_props = file_item.get("appProperties") or {}
    if not isinstance(app_props, dict):
        app_props = {}

    status = str(app_props.get("mm_status") or "").strip().lower()
    return status not in {"processed", "failed", "processing"}


def _download_drive_file(drive_service: Any, file_id: str, destination_path: Path) -> None:
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    request = drive_service.files().get_media(fileId=file_id, supportsAllDrives=True)
    with open(destination_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()


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
    if status == "processed":
        props["processed_at"] = now
    elif status == "failed":
        props["failed_at"] = now
        if error_message:
            props["mm_error"] = str(error_message)[:500]

    _set_drive_app_properties(drive_service, file_id=file_id, properties=props)

    if status_backend == "folder_move":
        target_folder_id = processed_folder_id if status == "processed" else failed_folder_id
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

    drive_service = _build_drive_service()
    all_files = _list_drive_m4a_files_once(drive_service=drive_service, folder_id=drive_folder_id)

    targets = []
    for _item in all_files:
        if _is_unprocessed(_item):
            targets.append(_item)
        else:
            _app_props = _item.get("appProperties") or {}
            _status = str(_app_props.get("mm_status") or "").strip().lower() or "(empty)"
            logger.info(
                "DRIVE_CRON_SKIP: file_id=%s file_name=%s reason=mm_status=%s",
                _item.get("id", ""),
                _item.get("name", ""),
                _status,
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

    for item in targets:
        file_id = str(item.get("id") or "").strip()
        file_name = str(item.get("name") or "").strip()
        safe_name = _safe_file_name(file_name)

        with tempfile.TemporaryDirectory(prefix="meeting_audio_") as tmp_dir:
            local_path = Path(tmp_dir) / safe_name

            try:
                logger.info("DRIVE_CRON_DOWNLOAD_START: file_id=%s file_name=%s", file_id, file_name)
                _download_drive_file(drive_service=drive_service, file_id=file_id, destination_path=local_path)

                _set_drive_app_properties(
                    drive_service=drive_service,
                    file_id=file_id,
                    properties={
                        "mm_status": "processing",
                        "processing_started_at": _now_iso(),
                    },
                )

                logger.info("DRIVE_CRON_PIPELINE_START: file_id=%s local_path=%s", file_id, local_path)
                run_pipeline_from_cli(str(local_path.resolve()), auto_selected_audio=False)

                # TODO: re-enable after resolving appProperties 124-byte 403
                # _record_drive_status(
                #     drive_service=drive_service,
                #     file_item=item,
                #     status="processed",
                #     status_backend=status_backend,
                #     processed_folder_id=processed_folder_id,
                #     failed_folder_id=failed_folder_id,
                # )
                processed_count += 1
                logger.info("DRIVE_CRON_PROCESSED: file_id=%s file_name=%s", file_id, file_name)
            except Exception as exc:
                failed_count += 1
                logger.exception("DRIVE_CRON_FAILED: file_id=%s file_name=%s reason=%s", file_id, file_name, exc)
                # TODO: re-enable after resolving appProperties 124-byte 403
                # try:
                #     _record_drive_status(
                #         drive_service=drive_service,
                #         file_item=item,
                #         status="failed",
                #         status_backend=status_backend,
                #         processed_folder_id=processed_folder_id,
                #         failed_folder_id=failed_folder_id,
                #         error_message=str(exc),
                #     )
                # except Exception as status_exc:
                #     logger.exception(
                #         "DRIVE_CRON_STATUS_RECORD_FAILED: file_id=%s reason=%s",
                #         file_id,
                #         status_exc,
                #     )

    logger.info(
        "DRIVE_CRON_DONE: processed=%s failed=%s scanned=%s",
        processed_count,
        failed_count,
        len(all_files),
    )

    return {
        "processed": processed_count,
        "failed": failed_count,
        "scanned": len(all_files),
        "targets": len(targets),
    }


def main() -> None:
    print("CRON START")
    logger.info("CRON START")

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