import argparse
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Set

from main import run_pipeline_from_cli
from utils.logger import get_logger

logger = get_logger(__name__)


def _build_drive_service():
    from googleapiclient.discovery import build
    from config.settings import settings

    credentials = settings.get_google_credentials()
    return build("drive", "v3", credentials=credentials)


def _list_drive_m4a_files(drive_service: Any, folder_id: str) -> List[Dict[str, Any]]:
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
                fields="nextPageToken,files(id,name,createdTime,modifiedTime,parents,mimeType)",
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


def _list_m4a_files(audio_dir: Path):
    return sorted([p for p in audio_dir.glob("*.m4a") if p.is_file()])


def _normalized_path(file_path: Path) -> str:
    return os.path.normcase(str(file_path.resolve()))


def _safe_file_name(name: str) -> str:
    invalid = '<>:"/\\|?*'
    safe = "".join("_" if c in invalid else c for c in str(name or "").strip())
    return safe or f"audio_{int(time.time())}.m4a"


def _is_ready(file_path: Path, min_age_seconds: float = 2.0) -> bool:
    try:
        return (time.time() - file_path.stat().st_mtime) >= min_age_seconds
    except FileNotFoundError:
        return False


def _move_to_processed(file_path: Path, processed_dir: Path) -> Path:
    processed_dir.mkdir(parents=True, exist_ok=True)

    destination = processed_dir / file_path.name
    if destination.exists():
        destination = processed_dir / f"{file_path.stem}_{int(time.time())}{file_path.suffix}"

    shutil.move(str(file_path), str(destination))
    return destination


def _move_to_failed(file_path: Path, failed_dir: Path) -> Path:
    failed_dir.mkdir(parents=True, exist_ok=True)

    destination = failed_dir / file_path.name
    if destination.exists():
        destination = failed_dir / f"{file_path.stem}_{int(time.time())}{file_path.suffix}"

    shutil.move(str(file_path), str(destination))
    return destination


def _load_drive_processed_state(state_file: Path) -> Dict[str, Set[str]]:
    if not state_file.exists():
        return {"processed_ids": set(), "failed_ids": set()}

    try:
        loaded = json.loads(state_file.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            return {"processed_ids": set(), "failed_ids": set()}

        processed_raw = loaded.get("processed_ids") or []
        failed_raw = loaded.get("failed_ids") or []
        processed_ids = {str(item).strip() for item in processed_raw if str(item).strip()}
        failed_ids = {str(item).strip() for item in failed_raw if str(item).strip()}
        return {"processed_ids": processed_ids, "failed_ids": failed_ids}
    except Exception:
        return {"processed_ids": set(), "failed_ids": set()}


def _save_drive_processed_state(state_file: Path, state: Dict[str, Set[str]]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "processed_ids": sorted(list(state.get("processed_ids") or set())),
        "failed_ids": sorted(list(state.get("failed_ids") or set())),
        "updated_at": int(time.time()),
    }
    state_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _download_drive_file(drive_service: Any, file_id: str, destination_path: Path) -> None:
    from googleapiclient.http import MediaIoBaseDownload

    destination_path.parent.mkdir(parents=True, exist_ok=True)

    request = drive_service.files().get_media(fileId=file_id, supportsAllDrives=True)
    with open(destination_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()


def watch_audio_folder(audio_dir: Path, poll_interval: float) -> None:
    audio_dir.mkdir(parents=True, exist_ok=True)
    processed_dir = audio_dir / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    failed_dir = audio_dir / "failed"
    failed_dir.mkdir(parents=True, exist_ok=True)

    startup_files = _list_m4a_files(audio_dir)
    for file_path in startup_files:
        if (processed_dir / file_path.name).exists():
            continue
        if not _is_ready(file_path):
            continue

        resolved = _normalized_path(file_path)
        logger.info("Startup existing audio detected. Start pipeline: %s", resolved)
        try:
            run_pipeline_from_cli(resolved, auto_selected_audio=False)
            moved_path = _move_to_processed(file_path, processed_dir)
            logger.info("Moved processed audio file: %s", moved_path)
        except Exception as exc:
            logger.exception("Pipeline failed for startup audio: %s", exc)
            try:
                failed_path = _move_to_failed(file_path, failed_dir)
                logger.error("Moved failed startup audio file: %s reason=%s", failed_path, exc)
            except Exception as move_exc:
                logger.exception("Failed to move startup audio to failed folder: %s", move_exc)

    seen: Set[str] = {_normalized_path(path) for path in _list_m4a_files(audio_dir)}
    logger.info("watching: %s", audio_dir.resolve())
    logger.info("Watching audio folder for new .m4a files: %s", audio_dir.resolve())

    while True:
        current_files = _list_m4a_files(audio_dir)

        for file_path in current_files:
            resolved = _normalized_path(file_path)
            if resolved in seen:
                continue
            if not _is_ready(file_path):
                continue

            logger.info("New audio detected. Start pipeline: %s", resolved)
            try:
                run_pipeline_from_cli(resolved, auto_selected_audio=False)
                moved_path = _move_to_processed(file_path, processed_dir)
                logger.info("Moved processed audio file: %s", moved_path)
            except Exception as exc:
                logger.exception("Pipeline failed for detected audio: %s", exc)
                try:
                    failed_path = _move_to_failed(file_path, failed_dir)
                    logger.error("Moved failed detected audio file: %s reason=%s", failed_path, exc)
                except Exception as move_exc:
                    logger.exception("Failed to move detected audio to failed folder: %s", move_exc)
            finally:
                seen.add(resolved)

        time.sleep(poll_interval)


def watch_drive_folder(
    audio_dir: Path,
    poll_interval: float,
    drive_folder_id: str,
    processed_state_file: Path,
) -> None:
    if not drive_folder_id:
        raise ValueError("drive_folder_id is required")

    audio_dir.mkdir(parents=True, exist_ok=True)
    processed_dir = audio_dir / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    failed_dir = audio_dir / "failed"
    failed_dir.mkdir(parents=True, exist_ok=True)

    state = _load_drive_processed_state(processed_state_file)
    processed_ids = state["processed_ids"]
    failed_ids = state["failed_ids"]

    logger.info("Watching Google Drive folder for .m4a files: folder_id=%s", drive_folder_id)
    logger.info("Drive processed state file: %s", processed_state_file)

    while True:
        try:
            drive_service = _build_drive_service()
            current_files = _list_drive_m4a_files(drive_service, drive_folder_id)
        except Exception as exc:
            logger.exception("Failed to poll Google Drive folder: %s", exc)
            time.sleep(poll_interval)
            continue

        for item in current_files:
            file_id = str(item.get("id") or "").strip()
            file_name = str(item.get("name") or "").strip()
            if not file_id:
                continue
            if file_id in processed_ids or file_id in failed_ids:
                continue

            local_file_name = _safe_file_name(file_name)
            local_path = audio_dir / local_file_name
            if local_path.exists():
                local_path = audio_dir / f"{Path(local_file_name).stem}_{int(time.time())}{Path(local_file_name).suffix}"

            logger.info("New Drive audio detected. Start download: file_id=%s name=%s", file_id, file_name)

            try:
                _download_drive_file(drive_service, file_id=file_id, destination_path=local_path)
                logger.info("Drive audio downloaded: file_id=%s local_path=%s", file_id, local_path)

                run_pipeline_from_cli(str(local_path.resolve()), auto_selected_audio=False)

                moved_path = _move_to_processed(local_path, processed_dir)
                logger.info("Moved processed Drive audio file: %s", moved_path)

                processed_ids.add(file_id)
                _save_drive_processed_state(
                    processed_state_file,
                    {"processed_ids": processed_ids, "failed_ids": failed_ids},
                )
            except Exception as exc:
                logger.exception("Pipeline failed for Drive audio: file_id=%s reason=%s", file_id, exc)

                if local_path.exists():
                    try:
                        failed_path = _move_to_failed(local_path, failed_dir)
                        logger.error("Moved failed Drive audio file: %s reason=%s", failed_path, exc)
                    except Exception as move_exc:
                        logger.exception("Failed to move Drive audio to failed folder: %s", move_exc)

                failed_ids.add(file_id)
                _save_drive_processed_state(
                    processed_state_file,
                    {"processed_ids": processed_ids, "failed_ids": failed_ids},
                )

        time.sleep(poll_interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Watch audio folder and auto-run pipeline")
    parser.add_argument(
        "--source",
        choices=["local", "drive"],
        default="local",
        help="Watch source type: local folder or Google Drive folder",
    )
    parser.add_argument(
        "--audio-dir",
        default="audio",
        help="Directory to watch for new .m4a files",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=3.0,
        help="Polling interval seconds",
    )
    parser.add_argument(
        "--drive-folder-id",
        default="",
        help="Google Drive folder ID to watch when --source drive",
    )
    parser.add_argument(
        "--drive-processed-state-file",
        default="data/drive_processed_files.json",
        help="State file path for processed/failed Google Drive file IDs",
    )

    args = parser.parse_args()

    base_dir = Path(__file__).resolve().parent
    audio_dir = Path(args.audio_dir)
    if not audio_dir.is_absolute():
        audio_dir = (base_dir / audio_dir).resolve()

    if args.source == "drive":
        drive_folder_id = str(args.drive_folder_id or "").strip()
        if not drive_folder_id:
            raise ValueError("--drive-folder-id is required when --source drive")

        processed_state_file = Path(args.drive_processed_state_file)
        if not processed_state_file.is_absolute():
            processed_state_file = (base_dir / processed_state_file).resolve()

        watch_drive_folder(
            audio_dir=audio_dir,
            poll_interval=args.poll_interval,
            drive_folder_id=drive_folder_id,
            processed_state_file=processed_state_file,
        )
        return

    watch_audio_folder(audio_dir=audio_dir, poll_interval=args.poll_interval)


if __name__ == "__main__":
    main()
