import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


BASE_DIR = Path(__file__).resolve().parent.parent
QA_SESSIONS_DIR = BASE_DIR / "data" / "qa_sessions"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_drive_folder_id(drive_folder_id: str) -> str:
    value = str(drive_folder_id or "").strip()
    return value.replace("/", "_").replace("\\", "_")


def _session_file_path(drive_folder_id: str) -> Path:
    QA_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    safe_id = _safe_drive_folder_id(drive_folder_id)
    return QA_SESSIONS_DIR / f"{safe_id}.json"


def _normalize_questions(raw: Any) -> List[str]:
    if not isinstance(raw, list):
        return []
    return [str(q).strip() for q in raw if str(q).strip()]


def _normalize_answers(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    normalized: List[Dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            normalized.append(item)
    return normalized


def _normalize_session_payload(raw: Dict[str, Any]) -> Dict[str, Any]:
    meeting_id = str(raw.get("meeting_id") or raw.get("drive_folder_id") or "").strip()
    ambiguity_questions = _normalize_questions(
        raw.get("ambiguity_questions")
        if raw.get("ambiguity_questions") is not None
        else raw.get("current_questions")
    )
    current_question_index = int(raw.get("current_question_index") or 0)
    if current_question_index < 0:
        current_question_index = 0
    answers = _normalize_answers(raw.get("answers"))

    status = str(raw.get("status") or "").strip()
    if not status:
        status = "completed" if current_question_index >= len(ambiguity_questions) else "waiting_for_answer"

    return {
        "meeting_id": meeting_id,
        "drive_folder_id": meeting_id,
        "docs_url": str(raw.get("docs_url") or "").strip(),
        "status": status,
        "close_reason": str(raw.get("close_reason") or "").strip(),
        "closed_at": str(raw.get("closed_at") or "").strip(),
        "ambiguity_questions": ambiguity_questions,
        "current_question_index": current_question_index,
        "answers": answers,
        "last_user_reply_at": str(
            raw.get("last_user_reply_at")
            or raw.get("last_answer_at")
            or ""
        ).strip(),
        "created_at": str(raw.get("created_at") or _now_iso()).strip(),
        "updated_at": str(raw.get("updated_at") or _now_iso()).strip(),
    }


def _is_session_active(session: Dict[str, Any]) -> bool:
    status = str(session.get("status") or "").strip().lower()
    if status in {"completed", "closed"}:
        return False

    questions = _normalize_questions(session.get("ambiguity_questions"))
    index = int(session.get("current_question_index") or 0)
    return 0 <= index < len(questions)


def create_or_reset_session(
    drive_folder_id: str,
    docs_url: str,
    questions: List[str],
) -> Dict[str, Any]:
    session = {
        "meeting_id": str(drive_folder_id or "").strip(),
        "drive_folder_id": str(drive_folder_id or "").strip(),
        "docs_url": str(docs_url or "").strip(),
        "status": "waiting_for_answer",
        "ambiguity_questions": [str(q).strip() for q in (questions or []) if str(q).strip()],
        "current_question_index": 0,
        "answers": [],
        "last_user_reply_at": "",
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    save_session(session)
    return session


def load_session(drive_folder_id: str) -> Optional[Dict[str, Any]]:
    path = _session_file_path(drive_folder_id)
    if not path.exists():
        return None

    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            normalized = _normalize_session_payload(loaded)
            if normalized != loaded:
                save_session(normalized)
            return normalized
    except Exception:
        return None
    return None


def save_session(session: Dict[str, Any]) -> None:
    normalized = _normalize_session_payload(session)
    drive_folder_id = str(normalized.get("meeting_id") or "").strip()
    if not drive_folder_id:
        raise ValueError("meeting_id is required")

    normalized["updated_at"] = _now_iso()

    path = _session_file_path(drive_folder_id)
    path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")


def find_latest_active_session() -> Optional[Dict[str, Any]]:
    QA_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    active: List[Dict[str, Any]] = []

    for path in QA_SESSIONS_DIR.glob("*.json"):
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue

        if not isinstance(loaded, dict):
            continue

        normalized = _normalize_session_payload(loaded)
        if _is_session_active(normalized):
            active.append(normalized)

    if not active:
        return None

    active.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)
    return active[0]


def find_active_session(meeting_id: str) -> Optional[Dict[str, Any]]:
    session = load_session(meeting_id)
    if not session:
        return None
    if not _is_session_active(session):
        return None
    return session


def close_all_active_sessions(close_reason: str) -> int:
    QA_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    closed_count = 0

    for path in QA_SESSIONS_DIR.glob("*.json"):
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue

        if not isinstance(loaded, dict):
            continue

        normalized = _normalize_session_payload(loaded)
        if not _is_session_active(normalized):
            continue

        normalized["status"] = "closed"
        normalized["close_reason"] = str(close_reason or "").strip()
        normalized["closed_at"] = _now_iso()
        save_session(normalized)
        closed_count += 1

    return closed_count


def get_current_question(session: Dict[str, Any]) -> str:
    questions = _normalize_questions(session.get("ambiguity_questions"))

    index = int(session.get("current_question_index") or 0)
    if index < 0 or index >= len(questions):
        return ""
    return str(questions[index] or "").strip()


def append_answer_and_advance(session: Dict[str, Any], answer_text: str) -> Dict[str, Any]:
    questions = _normalize_questions(session.get("ambiguity_questions"))
    session["ambiguity_questions"] = questions

    index = int(session.get("current_question_index") or 0)
    answers = _normalize_answers(session.get("answers"))
    session["answers"] = answers
    normalized_answer_text = str(answer_text or "").strip()
    is_skipped = normalized_answer_text == "スキップ"

    current_question = ""
    if 0 <= index < len(questions):
        current_question = str(questions[index] or "").strip()

    answers.append(
        {
            "question_index": index,
            "question": current_question,
            "answer": "" if is_skipped else normalized_answer_text,
            "is_skipped": is_skipped,
            "answered_at": _now_iso(),
        }
    )

    session["current_question_index"] = index + 1
    session["last_user_reply_at"] = _now_iso()
    session["status"] = (
        "ready_for_docs_update"
        if int(session.get("current_question_index") or 0) >= len(questions)
        else "waiting_for_answer"
    )
    save_session(session)
    reloaded = load_session(str(session.get("meeting_id") or session.get("drive_folder_id") or ""))
    return reloaded or _normalize_session_payload(session)
