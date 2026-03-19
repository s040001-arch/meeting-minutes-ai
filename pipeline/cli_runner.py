import json
import os
from urllib import request as urlrequest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from docs.google_docs_writer import save_latest_minutes_state
from line.qa_session_state import close_all_active_sessions, create_or_reset_session, get_current_question
from preprocess.transcript_preprocessor_gpt import detect_bottleneck_question
from pipeline.meeting_pipeline import run_meeting_pipeline
from utils.logger import get_logger

logger = get_logger(__name__)


def _build_validation_summary(validations: List[Dict[str, Any]]) -> Dict[str, Any]:
    total_stages = len(validations)
    warning_stages: List[Dict[str, Any]] = []

    for item in validations:
        count_ok = bool(item.get("count_ok", True))
        order_ok = bool(item.get("order_ok", True))
        if not count_ok or not order_ok:
            warning_stages.append(
                {
                    "stage": item.get("stage", "unknown"),
                    "input_count": item.get("input_count"),
                    "output_count": item.get("output_count"),
                    "issues": item.get("issues", []),
                }
            )

    return {
        "total_stages": total_stages,
        "warning_stage_count": len(warning_stages),
        "warning_stages": warning_stages,
        "all_ok": len(warning_stages) == 0,
    }


def _log_validation_summary_once(validations: List[Dict[str, Any]]) -> None:
    summary = _build_validation_summary(validations)

    if summary["all_ok"]:
        logger.info(
            "PIPELINE_UTTERANCE_VALIDATION_SUMMARY "
            + json.dumps(
                {
                    "status": "ok",
                    "total_stages": summary["total_stages"],
                    "warning_stage_count": 0,
                },
                ensure_ascii=False,
            )
        )
        return

    logger.warning(
        "PIPELINE_UTTERANCE_VALIDATION_SUMMARY "
        + json.dumps(
            {
                "status": "warning",
                "total_stages": summary["total_stages"],
                "warning_stage_count": summary["warning_stage_count"],
                "warning_stages": summary["warning_stages"],
            },
            ensure_ascii=False,
        )
    )


def _save_latest_meeting_state_after_success(result: Dict[str, Any]) -> None:
    meeting_info = result.get("meeting_info") or {}
    google_doc_result = result.get("google_doc_result") or {}
    latest_state = {
        "client": str(meeting_info.get("customer_name") or ""),
        "title": str(meeting_info.get("meeting_title") or ""),
        "docs_url": str(result.get("google_docs_url") or ""),
        "drive_folder_id": str(google_doc_result.get("folder_id") or ""),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        save_latest_minutes_state(latest_state)
        logger.info("latest_meeting.json saved after successful local batch execution")
    except TypeError:
        save_latest_minutes_state(
            google_docs_url=result.get("google_docs_url"),
            meeting_info=result.get("meeting_info"),
            minutes_text=result.get("formatted_minutes"),
        )
        logger.info("latest_meeting.json saved after successful local batch execution")
    except Exception as exc:
        logger.warning(f"Failed to save latest_meeting.json after successful run: {exc}")

    try:
        project_root = Path(__file__).resolve().parent.parent
        latest_meeting_file = project_root / "data" / "latest_meeting.json"
        latest_meeting_file.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "client": str(meeting_info.get("customer_name") or ""),
            "title": str(meeting_info.get("meeting_title") or ""),
            "docs_url": str(result.get("google_docs_url") or ""),
            "drive_folder_id": str(google_doc_result.get("folder_id") or ""),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        with open(latest_meeting_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.warning("Failed to write data/latest_meeting.json: %s", exc)


def _save_latest_minutes_markdown_after_success(result: Dict[str, Any]) -> None:
    try:
        project_root = Path(__file__).resolve().parent.parent
        state_file = project_root / "data" / "latest_minutes_state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        meeting_info = result.get("meeting_info") or {}

        payload = {
            "minutes_markdown": str(result.get("formatted_minutes") or ""),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "customer_name": str(meeting_info.get("customer_name") or ""),
            "meeting_title": str(meeting_info.get("meeting_title") or ""),
            "meeting_date": str(meeting_info.get("meeting_date") or ""),
            "local_minutes_file": str(result.get("local_minutes_path") or ""),
            "labeled_transcript": str(result.get("labeled_transcript") or ""),
        }
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.warning("Failed to save latest_minutes_state.json: %s", exc)


def _log_local_execution_summary_once(result: Dict[str, Any]) -> None:
    summary = result.get("execution_summary") or {}
    filename_fallback_used = bool(summary.get("filename_fallback_used", False))
    whisper_fallback_used = bool(summary.get("whisper_fallback_used", False))
    claude_fallback_used = bool(summary.get("claude_fallback_used", False))
    google_docs_fallback_used = bool(summary.get("google_docs_fallback_used", False))

    logger.info(
        "LOCAL_PIPELINE_EXECUTION_SUMMARY "
        + json.dumps(
            {
                "real_processing": {
                    "audio_load": True,
                    "transcription": not whisper_fallback_used,
                    "minutes_generation": not claude_fallback_used,
                    "google_docs_save": not google_docs_fallback_used,
                },
                "fallback": {
                    "filename": filename_fallback_used,
                    "transcription": whisper_fallback_used,
                    "minutes_generation": claude_fallback_used,
                    "google_docs_save": google_docs_fallback_used,
                },
                "google_docs_url": result.get("google_docs_url"),
            },
            ensure_ascii=False,
        )
    )


def _send_line_notification_after_success(result: Dict[str, Any]) -> None:
    logger.info("LINE_NOTIFY_START")
    try:
        docs_url = result.get("google_docs_url")
        if docs_url is None or str(docs_url).strip() == "":
            raise ValueError("google_docs_url is empty")
        docs_url = str(docs_url).strip()

        channel_access_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
        notify_user_id = os.getenv("LINE_NOTIFY_USER_ID", "").strip()
        if not channel_access_token or not notify_user_id:
            logger.warning("LINE_NOTIFY_SKIP: missing LINE_CHANNEL_ACCESS_TOKEN or LINE_NOTIFY_USER_ID")
            return

        meeting_info = result.get("meeting_info") or {}
        customer_name = str(meeting_info.get("customer_name") or "")
        meeting_title = str(meeting_info.get("meeting_title") or "")
        local_minutes_file = str(result.get("local_minutes_path") or "")
        initial_question = str(result.get("bottleneck_question") or "").strip()
        if not initial_question:
            initial_question = detect_bottleneck_question(
                cleaned_transcript=str(result.get("labeled_transcript") or ""),
                minutes_markdown=str(result.get("formatted_minutes") or ""),
                previous_answers=[],
                max_question_count=3,
            )

        message_text = (
            "議事録生成が完了しました\n"
            f"顧客名: {customer_name}\n"
            f"会議名: {meeting_title}\n"
            f"local_minutes_file: {local_minutes_file}\n"
            f"確認が必要な項目数: {1 if initial_question else 0}件\n"
            f"docs_url: {docs_url}"
        )

        payload = {
            "to": notify_user_id,
            "messages": [
                {
                    "type": "text",
                    "text": message_text[:4900],
                }
            ],
        }
        req = urlrequest.Request(
            "https://api.line.me/v2/bot/message/push",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {channel_access_token}",
            },
            method="POST",
        )
        with urlrequest.urlopen(req) as resp:
            if resp.status >= 400:
                logger.warning("LINE_NOTIFY_SKIP: HTTP status=%s", resp.status)
                return

        google_doc_result = result.get("google_doc_result") or {}
        drive_folder_id = str(google_doc_result.get("folder_id") or "").strip()
        logger.info(
            "LINE_QA_FLOW_START: initial_question_present=%s drive_folder_id_present=%s",
            bool(initial_question),
            bool(drive_folder_id),
        )

        if initial_question and drive_folder_id:
            session = create_or_reset_session(
                drive_folder_id=drive_folder_id,
                docs_url=docs_url,
                question=initial_question,
                labeled_transcript=str(result.get("labeled_transcript") or ""),
                max_questions=3,
            )
            logger.info(
                "LINE_QA_SESSION_CREATED: meeting_id=%s question_count=%s",
                str(session.get("meeting_id") or drive_folder_id),
                int(session.get("question_count") or 0),
            )
            first_question = get_current_question(session)
            if first_question:
                question_payload = {
                    "to": notify_user_id,
                    "messages": [
                        {
                            "type": "text",
                            "text": first_question[:4900],
                        }
                    ],
                }
                question_req = urlrequest.Request(
                    "https://api.line.me/v2/bot/message/push",
                    data=json.dumps(question_payload).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {channel_access_token}",
                    },
                    method="POST",
                )
                with urlrequest.urlopen(question_req) as question_resp:
                    if question_resp.status >= 400:
                        logger.warning("LINE_QA_FIRST_QUESTION_SKIP: HTTP status=%s", question_resp.status)
                    else:
                        logger.info(
                            "LINE_QA_FLOW_QUESTION_SENT: meeting_id=%s question_count=%s question=%s",
                            str(session.get("meeting_id") or drive_folder_id),
                            int(session.get("question_count") or 0),
                            first_question,
                        )

        logger.info("LINE_NOTIFY_SUCCESS: customer_name=%s meeting_title=%s", customer_name, meeting_title)
    except Exception as exc:
        logger.warning("LINE_NOTIFY_SKIP: exception=%s", exc)


def run_pipeline_from_cli(
    audio_file_path: str,
    auto_selected_audio: bool = False,
) -> Dict[str, Any]:
    logger.info(
        "CLI_PIPELINE_AUDIO_INPUT: path=%s extension=%s",
        audio_file_path,
        Path(audio_file_path).suffix.lower(),
    )

    close_reason = "次の会議開始による打ち切り"
    closed_count = close_all_active_sessions(close_reason)
    if closed_count > 0:
        logger.info(
            "LINE_QA_SESSIONS_CLOSED_ON_NEW_MEETING_START: count=%s reason=%s",
            closed_count,
            close_reason,
        )

    result = run_meeting_pipeline(audio_file_path)
    _save_latest_minutes_markdown_after_success(result)
    validations = result.get("utterance_validations", []) or []
    _log_validation_summary_once(validations)
    _save_latest_meeting_state_after_success(result)
    _log_local_execution_summary_once(result)
    _send_line_notification_after_success(result)
    local_minutes_path = result.get("local_minutes_path", "")
    if local_minutes_path:
        logger.info("LOCAL_PIPELINE_MINUTES_FILE %s", local_minutes_path)
        if os.path.exists(local_minutes_path):
            if os.name == "nt" and hasattr(os, "startfile"):
                os.startfile(local_minutes_path)
            else:
                logger.info("LOCAL_PIPELINE_MINUTES_OPEN_SKIP: non-windows environment path=%s", local_minutes_path)
    if auto_selected_audio:
        logger.info("LOCAL_PIPELINE_AUTO_SELECTED_AUDIO_PATH %s", audio_file_path)
    return result