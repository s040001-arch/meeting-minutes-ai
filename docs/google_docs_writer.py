import time
from typing import Any, Dict, Optional, Tuple

from googleapiclient.discovery import build

from config.settings import settings
from utils.logger import get_logger

logger = get_logger(__name__)

_DOCS_API_RETRY_MAX_ATTEMPTS = 4
_DOCS_API_RETRY_BASE_SECONDS = 0.7
_DOCS_GET_RETRY_MAX_ATTEMPTS = 6


def _is_permission_403(exc: Exception) -> bool:
    text = str(exc).lower()
    return "403" in text and "permission" in text


def _run_docs_call_with_retry(
    label: str,
    fn,
    max_attempts: int = _DOCS_API_RETRY_MAX_ATTEMPTS,
):
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        logger.info("GOOGLE_DOCS_API_CALL_ATTEMPT: call=%s attempt=%s", label, attempt)
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if not _is_permission_403(exc) or attempt >= max_attempts:
                logger.warning("GOOGLE_DOCS_API_CALL_FAILED: call=%s attempt=%s reason=%s", label, attempt, exc)
                raise

            sleep_seconds = _DOCS_API_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
            logger.warning(
                "GOOGLE_DOCS_API_CALL_RETRY: call=%s attempt=%s wait_seconds=%s reason=%s",
                label,
                attempt,
                round(sleep_seconds, 2),
                exc,
            )
            time.sleep(sleep_seconds)

    if last_exc:
        raise last_exc


def _parse_minutes_sections(text: str) -> Dict[str, list[str]]:
    sections: Dict[str, list[str]] = {
        "参加者": [],
        "会議概要": [],
        "決まったこと": [],
        "残論点": [],
        "Next Action": [],
        "発言録（逐語）": [],
    }
    current: Optional[str] = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            heading = line[3:].strip()
            if heading in sections:
                current = heading
            else:
                current = None
            continue
        if current and line:
            if line.startswith("- "):
                line = line[2:].strip()
            sections[current].append(line)

    return sections


def _build_styled_doc_payload(meeting_info: Dict[str, Any], minutes_text: str) -> Dict[str, Any]:
    meeting_date = str(meeting_info.get("date") or "")
    customer_name = str(meeting_info.get("customer_name") or "")
    meeting_title = str(meeting_info.get("meeting_title") or "")
    sections = _parse_minutes_sections(minutes_text)

    lines: list[str] = []
    title_line_indexes: list[int] = []
    heading_line_indexes: list[int] = []
    bullet_line_indexes: list[int] = []

    title_line_indexes.append(len(lines))
    lines.append(meeting_date)
    title_line_indexes.append(len(lines))
    lines.append(f"{customer_name} {meeting_title}".strip())
    lines.append("")

    ordered_headings = ["参加者", "会議概要", "決まったこと", "残論点", "Next Action", "発言録（逐語）"]
    for heading in ordered_headings:
        heading_line_indexes.append(len(lines))
        lines.append(heading)

        body_lines = sections.get(heading) or ["記載なし"]
        for item in body_lines:
            bullet_line_indexes.append(len(lines))
            lines.append(item)

        lines.append("")

    full_text = "\n".join(lines) + "\n"

    line_ranges: list[Tuple[int, int]] = []
    cursor = 1
    for line in lines:
        start = cursor
        end = start + len(line)
        line_ranges.append((start, end))
        cursor = end + 1

    return {
        "text": full_text,
        "title_ranges": [line_ranges[idx] for idx in title_line_indexes],
        "heading_ranges": [line_ranges[idx] for idx in heading_line_indexes],
        "bullet_ranges": [line_ranges[idx] for idx in bullet_line_indexes],
    }


def _build_drive_service():
    credentials = settings.get_google_drive_write_credentials()
    return build("drive", "v3", credentials=credentials)


def _build_docs_service():
    credentials = settings.get_google_oauth_credentials()
    logger.info("DOCS_AUTH_PATH: oauth")
    return build("docs", "v1", credentials=credentials)


def run_oauth_docs_create_test_once() -> None:
    logger.info("OAUTH_TEST_START: Testing Docs API with OAuth credentials")
    try:
        docs_service = _build_docs_service()
        test_result = docs_service.documents().create(
            body={"title": "OAuth Test Doc"}
        ).execute()
        test_doc_id = test_result.get("documentId", "")
        logger.info("OAUTH_TEST_SUCCESS: document_id=%s", test_doc_id)
    except Exception as oauth_test_exc:
        logger.warning("OAUTH_TEST_FAILED: error=%s", oauth_test_exc)


def _find_or_create_meeting_folder(
    drive_service: Any,
    folder_name: str,
    parent_folder_id: Optional[str] = None,
) -> str:
    query_parts = [
        "mimeType = 'application/vnd.google-apps.folder'",
        f"name = '{folder_name}'",
        "trashed = false",
    ]
    if parent_folder_id:
        query_parts.append(f"'{parent_folder_id}' in parents")

    response = drive_service.files().list(
        q=" and ".join(query_parts),
        spaces="drive",
        fields="files(id, name)",
        pageSize=10,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()

    files = response.get("files", [])
    if files:
        folder_id = files[0]["id"]
        logger.info("Reuse existing meeting folder: %s (%s)", folder_name, folder_id)
        return folder_id

    metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_folder_id:
        metadata["parents"] = [parent_folder_id]

    folder = drive_service.files().create(
        body=metadata,
        fields="id",
        supportsAllDrives=True,
    ).execute()

    folder_id = folder["id"]
    logger.info("Created meeting folder: %s (%s)", folder_name, folder_id)
    return folder_id


def _find_or_create_minutes_doc(
    drive_service: Any,
    docs_service: Any,
    doc_name: str,
    folder_id: str,
) -> Tuple[str, bool]:
    query = " and ".join(
        [
            "mimeType = 'application/vnd.google-apps.document'",
            f"name = '{doc_name}'",
            f"'{folder_id}' in parents",
            "trashed = false",
        ]
    )

    response = drive_service.files().list(
        q=query,
        spaces="drive",
        fields="files(id, name)",
        pageSize=10,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()

    files = response.get("files", [])
    if files:
        document_id = files[0]["id"]
        logger.info("Reuse existing Google Doc: %s (%s)", doc_name, document_id)
        return document_id, False

    logger.info("DOCS_CREATE_START: doc_name=%s folder_id=%s", doc_name, folder_id)
    try:
        document = docs_service.documents().create(
            body={"title": doc_name},
        ).execute()
        document_id = document["documentId"]
        logger.info("DOCS_CREATE_SUCCESS: document_id=%s", document_id)
    except Exception as exc:
        logger.warning("DOCS_CREATE_FAILED: reason=%s", exc)
        raise

    logger.info("DRIVE_PERMISSION_GRANT_START: document_id=%s folder_id=%s", document_id, folder_id)
    try:
        drive_service.files().update(
            fileId=document_id,
            addParents=folder_id,
            supportsAllDrives=True,
        ).execute()
        logger.info("DRIVE_PERMISSION_GRANT_SUCCESS: document_id=%s", document_id)
    except Exception as exc:
        logger.warning("DRIVE_PERMISSION_GRANT_FAILED: document_id=%s reason=%s", document_id, exc)
        raise

    logger.info("Created new Google Doc via Docs API (OAuth): %s (%s)", doc_name, document_id)
    return document_id, True


def _clear_document_content(docs_service: Any, document_id: str) -> None:
    logger.info("GOOGLE_DOCS_API_CALL_START: call=documents.get document_id=%s", document_id)
    document = _run_docs_call_with_retry(
        "documents.get",
        lambda: docs_service.documents().get(documentId=document_id).execute(),
        max_attempts=_DOCS_GET_RETRY_MAX_ATTEMPTS,
    )
    logger.info("GOOGLE_DOCS_API_CALL_OK: call=documents.get document_id=%s", document_id)
    body = document.get("body", {})
    content = body.get("content", [])

    if not content:
        return

    end_index = content[-1].get("endIndex", 1)
    if end_index <= 2:
        return

    logger.info("GOOGLE_DOCS_API_CALL_START: call=documents.batchUpdate.clear document_id=%s", document_id)
    _run_docs_call_with_retry(
        "documents.batchUpdate.clear",
        lambda: docs_service.documents().batchUpdate(
            documentId=document_id,
            body={
                "requests": [
                    {
                        "deleteContentRange": {
                            "range": {
                                "startIndex": 1,
                                "endIndex": end_index - 1,
                            }
                        }
                    }
                ]
            },
        ).execute(),
    )
    logger.info("GOOGLE_DOCS_API_CALL_OK: call=documents.batchUpdate.clear document_id=%s", document_id)

    logger.info("Cleared existing Google Doc content: %s", document_id)


def _write_document_text(
    docs_service: Any,
    document_id: str,
    text: str,
    meeting_info: Dict[str, Any],
) -> None:
    payload = _build_styled_doc_payload(meeting_info=meeting_info, minutes_text=text)

    logger.info("GOOGLE_DOCS_API_CALL_START: call=documents.batchUpdate.insert document_id=%s", document_id)
    _run_docs_call_with_retry(
        "documents.batchUpdate.insert",
        lambda: docs_service.documents().batchUpdate(
            documentId=document_id,
            body={
                "requests": [
                    {
                        "insertText": {
                            "location": {"index": 1},
                            "text": payload["text"],
                        }
                    }
                ]
            },
        ).execute(),
    )
    logger.info("GOOGLE_DOCS_API_CALL_OK: call=documents.batchUpdate.insert document_id=%s", document_id)

    style_requests = []
    for start, end in payload["title_ranges"]:
        style_requests.append(
            {
                "updateParagraphStyle": {
                    "range": {"startIndex": start, "endIndex": end},
                    "paragraphStyle": {"namedStyleType": "TITLE"},
                    "fields": "namedStyleType",
                }
            }
        )

    for start, end in payload["heading_ranges"]:
        style_requests.append(
            {
                "updateParagraphStyle": {
                    "range": {"startIndex": start, "endIndex": end},
                    "paragraphStyle": {"namedStyleType": "HEADING_1"},
                    "fields": "namedStyleType",
                }
            }
        )

    for start, end in payload["bullet_ranges"]:
        style_requests.append(
            {
                "createParagraphBullets": {
                    "range": {"startIndex": start, "endIndex": end},
                    "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
                }
            }
        )

    if style_requests:
        logger.info("GOOGLE_DOCS_API_CALL_START: call=documents.batchUpdate.style document_id=%s", document_id)
        _run_docs_call_with_retry(
            "documents.batchUpdate.style",
            lambda: docs_service.documents().batchUpdate(
                documentId=document_id,
                body={"requests": style_requests},
            ).execute(),
        )
        logger.info("GOOGLE_DOCS_API_CALL_OK: call=documents.batchUpdate.style document_id=%s", document_id)

    logger.info("Inserted latest formatted minutes into Google Doc: %s", document_id)


def write_minutes_to_google_docs(
    meeting_info: Dict[str, Any],
    minutes_text: str,
    audio_file_path: Optional[str] = None,
    existing_document_id: Optional[str] = None,
) -> Dict[str, str]:
    """
    minutes + transcript を合成した本文を同一document_idに一括反映する。
    """
    if not meeting_info:
        raise ValueError("meeting_info is required.")
    if not minutes_text or not minutes_text.strip():
        raise ValueError("minutes_text is empty.")

    meeting_date = meeting_info.get("date", "")
    customer_name = meeting_info.get("customer_name", "")
    meeting_title = meeting_info.get("meeting_title", "")

    folder_name = f"{meeting_date}_{customer_name}_{meeting_title}"
    doc_name = f"{folder_name}_議事録"

    if not getattr(settings, "ENABLE_GOOGLE_DOCS_WRITE", True):
        logger.info("Google Docs write skipped by ENABLE_GOOGLE_DOCS_WRITE=false (minutes+transcript)")
        dummy_document_id = "DUMMY_DOC_WRITE_DISABLED"
        dummy_url = f"https://docs.google.com/document/d/{dummy_document_id}/edit"
        return {
            "folder_id": "",
            "document_id": dummy_document_id,
            "document_url": dummy_url,
            "google_docs_url": dummy_url,
            "folder_name": folder_name,
            "document_name": doc_name,
            "created": "false",
        }

    # --- 必ず同一document_idに minutes+transcript 合成本文を反映 ---
    if existing_document_id:
        logger.info(
            "DOCS_COMBINED_MINUTES_TRANSCRIPT_SAVE_START: existing_document_id=%s folder_name=%s",
            existing_document_id, folder_name,
        )
        try:
            docs_service = _build_docs_service()
            _clear_document_content(docs_service=docs_service, document_id=existing_document_id)
            _write_document_text(
                docs_service=docs_service,
                document_id=existing_document_id,
                text=minutes_text.strip(),
                meeting_info=meeting_info,
            )
            document_url = f"https://docs.google.com/document/d/{existing_document_id}/edit"
            logger.info("DOCS_COMBINED_MINUTES_TRANSCRIPT_SAVE_DONE: document_id=%s", existing_document_id)
            return {
                "folder_id": "",
                "document_id": existing_document_id,
                "document_url": document_url,
                "google_docs_url": document_url,
                "folder_name": folder_name,
                "document_name": doc_name,
                "created": "false",
            }
        except Exception as exc:
            logger.warning(
                "MINUTES_DOCS_SAVE_FAILED: document_id=%s reason=%s (no fallback create)",
                existing_document_id, exc,
            )
            raise

    # fallback: 新規作成（通常は通らない）
    parent_folder_id = getattr(settings, "GOOGLE_DRIVE_BASE_FOLDER_ID", None)
    logger.info(
        "GOOGLE_DOCS_SAVE_CONTEXT: parent_folder_id=%s enable_google_docs_write=%s",
        str(parent_folder_id or ""),
        bool(getattr(settings, "ENABLE_GOOGLE_DOCS_WRITE", True)),
    )

    try:
        drive_service = _build_drive_service()
        docs_service = _build_docs_service()

        folder_id = _find_or_create_meeting_folder(
            drive_service=drive_service,
            folder_name=folder_name,
            parent_folder_id=parent_folder_id,
        )

        document_id, is_created = _find_or_create_minutes_doc(
            drive_service=drive_service,
            docs_service=docs_service,
            doc_name=doc_name,
            folder_id=folder_id,
        )

        _clear_document_content(docs_service=docs_service, document_id=document_id)
        _write_document_text(
            docs_service=docs_service,
            document_id=document_id,
            text=minutes_text.strip(),
            meeting_info=meeting_info,
        )

        document_url = f"https://docs.google.com/document/d/{document_id}/edit"

        return {
            "folder_id": folder_id,
            "document_id": document_id,
            "document_url": document_url,
            "folder_name": folder_name,
            "document_name": doc_name,
            "created": "true" if is_created else "false",
        }
    except Exception as exc:
        logger.warning(
            "MINUTES_DOCS_SAVE_FAILED: reason=%s", exc
        )
        error_text = str(exc).lower()
        has_permission_error = "permission" in error_text
        has_storage_quota_error = "storagequotaexceeded" in error_text
        if "403" in error_text and (has_permission_error or has_storage_quota_error):
            if has_permission_error:
                logger.warning("Google Docs write skipped due to permission error (403): %s", exc)
            if has_storage_quota_error:
                logger.warning("Google Docs write skipped due to storageQuotaExceeded (403): %s", exc)
            dummy_document_id = "DUMMY_DOC_PERMISSION_DENIED"
            dummy_url = f"https://docs.google.com/document/d/{dummy_document_id}/edit"
            return {
                "folder_id": "",
                "document_id": dummy_document_id,
                "document_url": dummy_url,
                "google_docs_url": dummy_url,
                "folder_name": folder_name,
                "document_name": doc_name,
                "created": "false",
            }
        raise
def save_latest_minutes_state(*args, **kwargs):
    return None


def write_transcript_to_google_docs(
    meeting_info: Dict[str, Any],
    transcript_text: str,
    audio_file_path: Optional[str] = None,
    existing_document_id: Optional[str] = None,
) -> Dict[str, str]:
    """
    逐語録（transcript）をGoogle Docsに保存・更新する。
    既存のDocsがあればupdate、なければcreate。
    """
    if not meeting_info:
        raise ValueError("meeting_info is required.")
    if not transcript_text or not transcript_text.strip():
        raise ValueError("transcript_text is empty.")

    meeting_date = meeting_info.get("date", "")
    customer_name = meeting_info.get("customer_name", "")
    meeting_title = meeting_info.get("meeting_title", "")

    folder_name = f"{meeting_date}_{customer_name}_{meeting_title}"
    doc_name = f"{folder_name}_逐語録"

    if not getattr(settings, "ENABLE_GOOGLE_DOCS_WRITE", True):
        logger.info("Google Docs write skipped by ENABLE_GOOGLE_DOCS_WRITE=false (transcript)")
        dummy_document_id = "DUMMY_DOC_WRITE_DISABLED"
        dummy_url = f"https://docs.google.com/document/d/{dummy_document_id}/edit"
        return {
            "folder_id": "",
            "document_id": dummy_document_id,
            "document_url": dummy_url,
            "google_docs_url": dummy_url,
            "folder_name": folder_name,
            "document_name": doc_name,
            "created": "false",
        }

    # --- Fast path: update existing doc directly ---
    if existing_document_id:
        logger.info(
            "TRANSCRIPT_DOCS_UPDATE_TARGET: existing_document_id=%s folder_name=%s (no new folder or doc created)",
            existing_document_id, folder_name,
        )
        try:
            docs_service = _build_docs_service()
            _clear_document_content(docs_service=docs_service, document_id=existing_document_id)
            _write_document_text(
                docs_service=docs_service,
                document_id=existing_document_id,
                text=transcript_text.strip(),
                meeting_info=meeting_info,
            )
            document_url = f"https://docs.google.com/document/d/{existing_document_id}/edit"
            logger.info("TRANSCRIPT_DOCS_UPDATE_EXISTING_SUCCESS: document_id=%s", existing_document_id)
            return {
                "folder_id": "",
                "document_id": existing_document_id,
                "document_url": document_url,
                "google_docs_url": document_url,
                "folder_name": folder_name,
                "document_name": doc_name,
                "created": "false",
            }
        except Exception as exc:
            logger.warning(
                "TRANSCRIPT_DOCS_UPDATE_EXISTING_FAILED: document_id=%s reason=%s (no fallback create)",
                existing_document_id, exc,
            )
            raise

    parent_folder_id = getattr(settings, "GOOGLE_DRIVE_BASE_FOLDER_ID", None)
    logger.info(
        "GOOGLE_DOCS_TRANSCRIPT_SAVE_CONTEXT: parent_folder_id=%s enable_google_docs_write=%s",
        str(parent_folder_id or ""),
        bool(getattr(settings, "ENABLE_GOOGLE_DOCS_WRITE", True)),
    )

    try:
        drive_service = _build_drive_service()
        docs_service = _build_docs_service()

        folder_id = _find_or_create_meeting_folder(
            drive_service=drive_service,
            folder_name=folder_name,
            parent_folder_id=parent_folder_id,
        )

        doc_name = f"{folder_name}_逐語録"
        document_id, is_created = _find_or_create_minutes_doc(
            drive_service=drive_service,
            docs_service=docs_service,
            doc_name=doc_name,
            folder_id=folder_id,
        )

        _clear_document_content(docs_service=docs_service, document_id=document_id)
        _write_document_text(
            docs_service=docs_service,
            document_id=document_id,
            text=transcript_text.strip(),
            meeting_info=meeting_info,
        )

        document_url = f"https://docs.google.com/document/d/{document_id}/edit"

        return {
            "folder_id": folder_id,
            "document_id": document_id,
            "document_url": document_url,
            "folder_name": folder_name,
            "document_name": doc_name,
            "created": "true" if is_created else "false",
        }
    except Exception as exc:
        error_text = str(exc).lower()
        has_permission_error = "permission" in error_text
        has_storage_quota_error = "storagequotaexceeded" in error_text
        if "403" in error_text and (has_permission_error or has_storage_quota_error):
            if has_permission_error:
                logger.warning("Google Docs transcript write skipped due to permission error (403): %s", exc)
            if has_storage_quota_error:
                logger.warning("Google Docs transcript write skipped due to storageQuotaExceeded (403): %s", exc)
            dummy_document_id = "DUMMY_DOC_PERMISSION_DENIED"
            dummy_url = f"https://docs.google.com/document/d/{dummy_document_id}/edit"
            return {
                "folder_id": "",
                "document_id": dummy_document_id,
                "document_url": dummy_url,
                "google_docs_url": dummy_url,
                "folder_name": folder_name,
                "document_name": doc_name,
                "created": "false",
            }
        raise