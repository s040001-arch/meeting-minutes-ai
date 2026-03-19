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
    ambiguity_questions = _normalize_questions(raw.get("ambiguity_questions"))
    legacy_questions = _normalize_questions(raw.get("current_questions"))
    if not ambiguity_questions and legacy_questions:
        ambiguity_questions = legacy_questions

    current_question = str(raw.get("current_question") or "").strip()
    answered = bool(raw.get("answered", False))

    current_question_index = int(raw.get("current_question_index") or 0)
    if current_question_index < 0:
        current_question_index = 0

    max_questions = int(raw.get("max_questions") or 3)
    if max_questions <= 0:
        max_questions = 3

    question_count = int(raw.get("question_count") or 0)
    if question_count < 0:
        question_count = 0

    if not current_question and ambiguity_questions and 0 <= current_question_index < len(ambiguity_questions):
        current_question = str(ambiguity_questions[current_question_index] or "").strip()

    if question_count == 0:
        if current_question:
            question_count = max(1, current_question_index + 1)
        elif ambiguity_questions:
            question_count = min(len(ambiguity_questions), max_questions)

    answers = _normalize_answers(raw.get("answers"))

    status = str(raw.get("status") or "").strip()
    if not status:
        if current_question and not answered and question_count <= max_questions:
            status = "waiting_for_answer"
        elif question_count >= max_questions or answers:
            status = "completed"
        else:
            status = "ready_for_question"

    return {
        "meeting_id": meeting_id,
        "drive_folder_id": meeting_id,
        "docs_url": str(raw.get("docs_url") or "").strip(),
        "status": status,
        "close_reason": str(raw.get("close_reason") or "").strip(),
        "closed_at": str(raw.get("closed_at") or "").strip(),
        "ambiguity_questions": ambiguity_questions,
        "current_question": current_question,
        "answered": answered,
        "question_count": question_count,
        "max_questions": max_questions,
        "current_question_index": current_question_index,
        "answers": answers,
        "labeled_transcript": str(raw.get("labeled_transcript") or "").strip(),
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

    current_question = str(session.get("current_question") or "").strip()
    answered = bool(session.get("answered", False))
    question_count = int(session.get("question_count") or 0)
    max_questions = int(session.get("max_questions") or 3)
    return bool(current_question) and not answered and question_count <= max_questions


def create_or_reset_session(
    drive_folder_id: str,
    docs_url: str,
    question: str,
    labeled_transcript: str = "",
    max_questions: int = 3,
) -> Dict[str, Any]:
    normalized_question = str(question or "").strip()
    session = {
        "meeting_id": str(drive_folder_id or "").strip(),
        "drive_folder_id": str(drive_folder_id or "").strip(),
        "docs_url": str(docs_url or "").strip(),
        "status": "waiting_for_answer" if normalized_question else "completed",
        "ambiguity_questions": [normalized_question] if normalized_question else [],
        "current_question": normalized_question,
        "answered": False,
        "question_count": 1 if normalized_question else 0,
        "max_questions": max(1, int(max_questions or 3)),
        "current_question_index": 0,
        "answers": [],
        "labeled_transcript": str(labeled_transcript or "").strip(),
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
    return str(session.get("current_question") or "").strip()


def append_answer_and_advance(session: Dict[str, Any], answer_text: str) -> Dict[str, Any]:
    current_question = str(session.get("current_question") or "").strip()
    if not current_question:
        raise ValueError("current_question is empty")

    if bool(session.get("answered", False)):
        raise ValueError("current question is already answered")

    index = int(session.get("question_count") or 1) - 1
    if index < 0:
        index = 0
    answers = _normalize_answers(session.get("answers"))
    session["answers"] = answers
    normalized_answer_text = str(answer_text or "").strip()
    is_skipped = normalized_answer_text == "スキップ"

    answers.append(
        {
            "question_index": index,
            "question": current_question,
            "answer": "" if is_skipped else normalized_answer_text,
            "is_skipped": is_skipped,
            "answered_at": _now_iso(),
        }
    )

    session["answered"] = True
    session["last_user_reply_at"] = _now_iso()
    session["status"] = "ready_for_docs_update"
    save_session(session)
    reloaded = load_session(str(session.get("meeting_id") or session.get("drive_folder_id") or ""))
    return reloaded or _normalize_session_payload(session)


def set_next_question(session: Dict[str, Any], question: str) -> Dict[str, Any]:
    next_question = str(question or "").strip()
    if not next_question:
        session["current_question"] = ""
        session["answered"] = True
        session["status"] = "completed"
        save_session(session)
        return load_session(str(session.get("meeting_id") or session.get("drive_folder_id") or "")) or _normalize_session_payload(session)

    max_questions = int(session.get("max_questions") or 3)
    question_count = int(session.get("question_count") or 0)
    if question_count >= max_questions:
        session["status"] = "completed"
        session["current_question"] = ""
        session["answered"] = True
        save_session(session)
        return load_session(str(session.get("meeting_id") or session.get("drive_folder_id") or "")) or _normalize_session_payload(session)

    session["current_question"] = next_question
    session["answered"] = False
    session["status"] = "waiting_for_answer"
    session["question_count"] = question_count + 1
    session["current_question_index"] = int(session.get("question_count") or 1) - 1

    history = _normalize_questions(session.get("ambiguity_questions"))
    history.append(next_question)
    session["ambiguity_questions"] = history

    save_session(session)
    return load_session(str(session.get("meeting_id") or session.get("drive_folder_id") or "")) or _normalize_session_payload(session)
