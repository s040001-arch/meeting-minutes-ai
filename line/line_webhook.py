import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib import request as urlrequest

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import Response
from openai import OpenAI

from config.settings import settings
from docs.google_docs_writer import write_minutes_to_google_docs
from line.qa_session_state import (
    append_answer_and_advance,
    find_active_session,
    find_latest_active_session,
    get_current_question,
    has_sent_question,
    mark_question_sent,
    save_session,
    set_next_question,
)
from preprocess.transcript_preprocessor_gpt import detect_bottleneck_question
from utils.logger import get_logger

logger = get_logger(__name__)

app = FastAPI()

_LINE_REPLY_ENDPOINT = "https://api.line.me/v2/bot/message/reply"
_LINE_PUSH_ENDPOINT = "https://api.line.me/v2/bot/message/push"
_LATEST_MINUTES_FILE = (Path(__file__).resolve().parent / ".." / "data" / "latest_minutes_state.json").resolve()
_OPENAI_MODEL = "gpt-4.1-mini"

client = OpenAI(api_key=settings.OPENAI_API_KEY)


@app.get("/test")
async def test_latest_minutes() -> Dict[str, Any]:
    state_json: Dict[str, Any] = {}
    try:
        if _LATEST_MINUTES_FILE.exists():
            with open(_LATEST_MINUTES_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                state_json = loaded
    except Exception:
        state_json = {}
    print(state_json)

    minutes_markdown = _load_latest_minutes_markdown()
    preview = minutes_markdown[:500]
    preview = preview.replace("\\n", "\n").replace("\\\\n", "\n")
    return Response(preview, media_type="text/plain")


def _load_latest_minutes_markdown() -> str:
    if not _LATEST_MINUTES_FILE.exists():
        return ""

    try:
        with open(_LATEST_MINUTES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return ""
        return str(data.get("minutes_markdown") or "").strip()
    except Exception as exc:
        logger.warning("Failed to read latest minutes state: %s", exc)
        return ""


def _save_latest_minutes_markdown(minutes_markdown: str, metadata: Dict[str, str]) -> None:
    payload = {
        "minutes_markdown": str(minutes_markdown or "").strip(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "customer_name": str(metadata.get("customer_name") or "").strip(),
        "meeting_title": str(metadata.get("meeting_title") or "").strip(),
        "meeting_date": str(metadata.get("meeting_date") or "").strip(),
        "local_minutes_file": str(metadata.get("local_minutes_file") or "").strip(),
        "labeled_transcript": str(metadata.get("labeled_transcript") or "").strip(),
    }
    _LATEST_MINUTES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_LATEST_MINUTES_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _load_latest_minutes_metadata() -> Dict[str, str]:
    metadata: Dict[str, str] = {
        "customer_name": "",
        "meeting_title": "",
        "meeting_date": "",
        "local_minutes_file": "",
        "labeled_transcript": "",
    }

    if not _LATEST_MINUTES_FILE.exists():
        return metadata

    try:
        with open(_LATEST_MINUTES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return metadata

        metadata["customer_name"] = str(data.get("customer_name") or "").strip()
        metadata["meeting_title"] = str(data.get("meeting_title") or "").strip()
        metadata["meeting_date"] = str(data.get("meeting_date") or "").strip()
        metadata["local_minutes_file"] = str(data.get("local_minutes_file") or "").strip()
        metadata["labeled_transcript"] = str(data.get("labeled_transcript") or "").strip()
        return metadata
    except Exception as exc:
        logger.warning("Failed to read latest minutes metadata: %s", exc)
        return metadata


def _load_latest_meeting_id() -> str:
    latest_meeting_file = (Path(__file__).resolve().parent / ".." / "data" / "latest_meeting.json").resolve()
    if not latest_meeting_file.exists():
        return ""

    try:
        with open(latest_meeting_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return ""
        return str(data.get("drive_folder_id") or data.get("meeting_id") or "").strip()
    except Exception as exc:
        logger.warning("Failed to read latest meeting id: %s", exc)
        return ""


def _build_prompt(question: str, minutes_markdown: str, metadata: Dict[str, str]) -> str:
    return f"""あなたは議事録Q&Aアシスタントです。
以下の最新議事録Markdownのみを根拠に、日本語で簡潔に回答してください。
議事録にない内容は「記載なし」と明示してください。

[会議メタ情報]
- customer_name: {metadata.get("customer_name", "")}
- meeting_title: {metadata.get("meeting_title", "")}
- meeting_date: {metadata.get("meeting_date", "")}
- local_minutes_file: {metadata.get("local_minutes_file", "")}

[最新議事録Markdown]
{minutes_markdown}

[ユーザー質問]
{question}
"""


def _generate_answer(question: str, minutes_markdown: str) -> str:
    if not minutes_markdown:
        return "最新議事録が見つかりませんでした。"

    metadata = _load_latest_minutes_metadata()
    prompt = _build_prompt(question, minutes_markdown, metadata)
    response = client.responses.create(
        model=_OPENAI_MODEL,
        input=prompt,
    )

    answer = str(getattr(response, "output_text", "") or "").strip()
    if not answer:
        return "回答を生成できませんでした。"

    return answer[:4900]


def _build_regeneration_prompt(
    original_minutes: str,
    metadata: Dict[str, str],
    answers: Any,
) -> str:
    answers_text = "\n".join(
        [
            (
                f"- Q{int(item.get('question_index', 0)) + 1}: {str(item.get('question') or '').strip()}\n"
                f"  A: {str(item.get('answer') or '').strip()}"
            )
            for item in (answers or [])
            if isinstance(item, dict)
            and not bool(item.get("is_skipped", False))
        ]
    )

    return f"""あなたは議事録編集アシスタントです。
元の議事録Markdownと、ユーザー回答を使って議事録を更新してください。

要件:
- 元文字起こしは使わない
- 元議事録の構成と文脈を維持する
- 回答内容のみを反映して不足情報を補完する
- 推測で新事実を追加しない
- 出力はMarkdown本文のみ
- 回答は要約せず、意味を保持して自然な文として反映する
- 回答に含まれる情報を削除しない

[会議メタ情報]
- customer_name: {metadata.get("customer_name", "")}
- meeting_title: {metadata.get("meeting_title", "")}
- meeting_date: {metadata.get("meeting_date", "")}

[元議事録Markdown]
{original_minutes}

[ユーザー回答]
{answers_text or '- なし'}
"""


def _regenerate_minutes_with_answers(
    original_minutes: str,
    metadata: Dict[str, str],
    answers: Any,
) -> str:
    logger.info(
        "LINE_QA_REGENERATE_START: answers=%s customer_name=%s meeting_title=%s",
        len(answers or []),
        metadata.get("customer_name", ""),
        metadata.get("meeting_title", ""),
    )
    prompt = _build_regeneration_prompt(
        original_minutes=original_minutes,
        metadata=metadata,
        answers=answers,
    )
    response = client.responses.create(
        model=_OPENAI_MODEL,
        input=prompt,
    )
    text = str(getattr(response, "output_text", "") or "").strip()
    if not text:
        raise ValueError("Minutes regeneration result is empty")
    logger.info("LINE_QA_REGENERATE_SUCCESS: updated_markdown_length=%s", len(text))
    return text


def _apply_answers_and_update_docs(session: Dict[str, Any]) -> str:
    original_minutes = _load_latest_minutes_markdown()
    if not original_minutes:
        raise ValueError("latest minutes markdown is empty")

    metadata = _load_latest_minutes_metadata()
    effective_answers = [
        item
        for item in (session.get("answers") or [])
        if isinstance(item, dict) and not bool(item.get("is_skipped", False))
    ]

    updated_markdown = _regenerate_minutes_with_answers(
        original_minutes=original_minutes,
        metadata=metadata,
        answers=effective_answers,
    )

    meeting_info = {
        "date": str(metadata.get("meeting_date") or "").strip(),
        "customer_name": str(metadata.get("customer_name") or "").strip(),
        "meeting_title": str(metadata.get("meeting_title") or "").strip(),
    }
    meeting_id_for_log = str(session.get("meeting_id") or session.get("drive_folder_id") or "")
    existing_docs_url = str(session.get("docs_url") or "").strip()
    existing_doc_id = ""
    if "/document/d/" in existing_docs_url:
        _parts = existing_docs_url.split("/document/d/", 1)
        if len(_parts) > 1:
            existing_doc_id = _parts[1].split("/")[0].strip()
    logger.info(
        "DOCS_UPDATE_TARGET_RESOLVE: meeting_id=%s existing_docs_url=%s existing_doc_id=%s",
        meeting_id_for_log, existing_docs_url, existing_doc_id,
    )
    docs_result = write_minutes_to_google_docs(
        meeting_info=meeting_info,
        minutes_text=updated_markdown,
        existing_document_id=existing_doc_id or None,
    )
    docs_url = str(
        docs_result.get("document_url")
        or docs_result.get("google_docs_url")
        or session.get("docs_url")
        or ""
    ).strip()

    _save_latest_minutes_markdown(updated_markdown, metadata)

    session["docs_url"] = docs_url
    save_session(session)
    logger.info(
        "DOCS_UPDATE_SUCCESS: meeting_id=%s docs_url=%s",
        str(session.get("meeting_id") or session.get("drive_folder_id") or ""),
        docs_url,
    )
    return docs_url


def _generate_next_bottleneck_question(
    session: Dict[str, Any],
    updated_minutes: str,
    extra_sent_questions: Optional[list] = None,
) -> str:
    transcript_text = str(session.get("labeled_transcript") or "").strip()
    if not transcript_text:
        metadata = _load_latest_minutes_metadata()
        transcript_text = str(metadata.get("labeled_transcript") or "").strip()

    answers = list(session.get("answers") or [])
    # Inject already-tried duplicate candidates as pseudo-answers so LLM avoids them
    for q in (extra_sent_questions or []):
        answers.append({
            "question_index": len(answers),
            "question": str(q),
            "answer": "（確認済み・再送不可）",
            "is_skipped": False,
        })

    return detect_bottleneck_question(
        cleaned_transcript=transcript_text,
        minutes_markdown=updated_minutes,
        previous_answers=answers,
        max_question_count=int(session.get("max_questions") or 3),
    )


def _find_target_active_session() -> Optional[Dict[str, Any]]:
    latest_meeting_id = _load_latest_meeting_id()
    if latest_meeting_id:
        scoped = find_active_session(latest_meeting_id)
        if scoped:
            return scoped
    return find_latest_active_session()


def _handle_ambiguity_answer(question_answer: str) -> str:
    session = _find_target_active_session()
    if not session:
        return "アクティブな確認セッションが見つかりませんでした。"

    status = str(session.get("status") or "").strip()
    if status != "waiting_for_answer":
        logger.warning(
            "LINE_QA_FLOW_WAITING_NO_ACTION: meeting_id=%s status=%s",
            str(session.get("meeting_id") or session.get("drive_folder_id") or ""),
            status,
        )
        return "現在このセッションは回答待機中ではありません。次の音声処理をお待ちください。"

    if bool(session.get("answered", False)):
        logger.warning(
            "LINE_QA_FLOW_WAITING_NO_ACTION: meeting_id=%s status=answered_flag_true",
            str(session.get("meeting_id") or session.get("drive_folder_id") or ""),
        )
        return "現在の質問への回答処理中です。少し待ってから再送してください。"

    meeting_id = str(session.get("meeting_id") or session.get("drive_folder_id") or "")
    answered_count = int(session.get("question_count") or 0)
    max_questions = int(session.get("max_questions") or 3)
    logger.info(
        "LINE_QA_FLOW_ANSWER_RECEIVED: meeting_id=%s question_number=%s max=%s answer=%s",
        meeting_id,
        answered_count,
        max_questions,
        question_answer,
    )

    # Record answer (status → "answered")
    updated = append_answer_and_advance(session, question_answer)
    question_count = int(updated.get("question_count") or 0)

    # Load original minutes BEFORE any docs update
    original_minutes = _load_latest_minutes_markdown()

    # --- Termination check 1: max questions reached ---
    if question_count >= max_questions:
        logger.info(
            "LINE_QA_DOCS_UPDATE_TRIGGERED: meeting_id=%s reason=max_questions question_number=%s",
            meeting_id, question_count,
        )
        docs_url = _apply_answers_and_update_docs(updated)
        updated["status"] = "completed"
        save_session(updated)
        logger.info("LINE_QA_FLOW_COMPLETED: reason=max_questions docs_url=%s", docs_url)
        return f"✅ 議事録を更新しました\ndocs_url: {docs_url}\n\n全質問完了です（{question_count}問）。"

    # --- Find next unsent question, skipping duplicate candidates via retry ---
    tried_duplicates: list = []
    max_retry = max(1, max_questions - question_count)
    next_question = ""
    for retry_idx in range(max_retry + 1):
        candidate = _generate_next_bottleneck_question(
            updated,
            original_minutes,
            extra_sent_questions=tried_duplicates if tried_duplicates else None,
        )
        if not candidate:
            logger.info(
                "LINE_QA_NO_MORE_BOTTLENECK: meeting_id=%s retry_idx=%s",
                meeting_id, retry_idx,
            )
            break
        if "\n" in candidate or "\r" in candidate:
            logger.warning("LINE_QA_FLOW_ERROR_MULTIPLE_QUESTION: raw=%s", candidate)
            candidate = candidate.replace("\r", " ").replace("\n", " ").strip()
        if has_sent_question(updated, candidate):
            logger.info(
                "LINE_QA_DUPLICATE_CANDIDATE_DETECTED: meeting_id=%s attempt=%s question=%s",
                meeting_id, retry_idx + 1, candidate,
            )
            tried_duplicates.append(candidate)
            continue
        logger.info(
            "LINE_QA_NEXT_UNSENT_CANDIDATE_SELECTED: meeting_id=%s attempt=%s question=%s",
            meeting_id, retry_idx + 1, candidate,
        )
        next_question = candidate
        break

    # --- Termination: no unsent candidate found ---
    if not next_question:
        reason = "duplicate_exhausted" if tried_duplicates else "no_more_bottleneck"
        if tried_duplicates:
            logger.info(
                "LINE_QA_FLOW_DUPLICATE_QUESTION_SKIPPED: meeting_id=%s all_tried=%s",
                meeting_id, tried_duplicates,
            )
        logger.info(
            "LINE_QA_DOCS_UPDATE_TRIGGERED: meeting_id=%s reason=%s question_number=%s",
            meeting_id, reason, question_count,
        )
        docs_url = _apply_answers_and_update_docs(updated)
        updated["status"] = "completed"
        updated["current_question"] = ""
        updated["answered"] = True
        save_session(updated)
        logger.info("LINE_QA_FLOW_COMPLETED: reason=%s docs_url=%s", reason, docs_url)
        return f"✅ 議事録を更新しました\ndocs_url: {docs_url}\n\n全質問完了です。"

    # --- Continue: set next question, NO docs update ---
    logger.info(
        "LINE_QA_DOCS_UPDATE_SKIPPED: meeting_id=%s reason=continuing question_number=%s",
        meeting_id, question_count,
    )
    refreshed = set_next_question(updated, next_question)
    sent_question = get_current_question(refreshed)
    refreshed = mark_question_sent(refreshed, sent_question)
    next_count = int(refreshed.get("question_count") or 0)
    logger.info(
        "LINE_QA_FLOW_QUESTION_SENT: meeting_id=%s question_number=%s/%s question=%s",
        meeting_id,
        next_count,
        max_questions,
        sent_question,
    )
    return f"次の確認質問です（{next_count}/{max_questions}問目）。\n\n{sent_question}"


def _reply_to_line(reply_token: str, message_text: str) -> None:
    access_token = settings.LINE_CHANNEL_ACCESS_TOKEN
    if not access_token:
        raise HTTPException(status_code=500, detail="LINE_CHANNEL_ACCESS_TOKEN is not set")

    payload = {
        "replyToken": reply_token,
        "messages": [
            {
                "type": "text",
                "text": message_text,
            }
        ],
    }

    body = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(
        _LINE_REPLY_ENDPOINT,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
        method="POST",
    )

    logger.info("LINE_QA_REPLY_MESSAGE_SEND_START: reply_token_present=%s", bool(reply_token))
    with urlrequest.urlopen(req) as resp:
        if resp.status >= 400:
            raise HTTPException(status_code=500, detail="Failed to reply to LINE")
    logger.info("LINE_QA_REPLY_MESSAGE_SEND_SUCCESS: status=ok")


def _push_to_line_user(user_id: str, message_text: str) -> None:
    access_token = settings.LINE_CHANNEL_ACCESS_TOKEN
    if not access_token:
        raise HTTPException(status_code=500, detail="LINE_CHANNEL_ACCESS_TOKEN is not set")

    payload = {
        "to": str(user_id or "").strip(),
        "messages": [
            {
                "type": "text",
                "text": str(message_text or "")[:4900],
            }
        ],
    }

    body = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(
        _LINE_PUSH_ENDPOINT,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
        method="POST",
    )

    logger.info("LINE_QA_PUSH_MESSAGE_SEND_START: user_id_present=%s", bool(str(user_id or "").strip()))
    with urlrequest.urlopen(req) as resp:
        if resp.status >= 400:
            raise HTTPException(status_code=500, detail="Failed to push to LINE")
    logger.info("LINE_QA_PUSH_MESSAGE_SEND_SUCCESS: status=ok")


def _process_callback_events(body: Dict[str, Any]) -> None:
    events = body.get("events", []) if isinstance(body, dict) else []
    minutes_markdown = _load_latest_minutes_markdown()

    for event in events:
        if not isinstance(event, dict):
            continue
        source = event.get("source") or {}
        user_id = str(source.get("userId") or "").strip()
        if user_id:
            logger.info("LINE_USER_ID: %s", user_id)
        if event.get("type") != "message":
            continue

        message = event.get("message") or {}
        if message.get("type") != "text":
            continue

        reply_token = str(event.get("replyToken") or "").strip()
        question = str(message.get("text") or "").strip()
        if not reply_token or not question:
            continue

        answer = ""
        active_session = None
        is_qa_answer_flow = False
        immediate_sent = False
        try:
            active_session = _find_target_active_session()
            if active_session is not None:
                is_qa_answer_flow = True
                if user_id:
                    _reply_to_line(reply_token, "⏳ 処理中です（数分かかる場合があります）")
                    immediate_sent = True
                answer = _handle_ambiguity_answer(question)
            else:
                answer = _generate_answer(question, minutes_markdown)
        except Exception as exc:
            logger.warning("OpenAI answer generation failed: %s", exc)
            answer = "⚠️ エラーが発生しました（再試行してください）"
        finally:
            if not str(answer or "").strip():
                answer = "⚠️ エラーが発生しました（再試行してください）"

            logger.info(
                "LINE_QA_REPLY_MESSAGE_PREPARED: answer_length=%s active_session=%s",
                len(str(answer or "")),
                bool(active_session is not None),
            )
            try:
                if is_qa_answer_flow and immediate_sent and user_id:
                    _push_to_line_user(user_id, str(answer)[:4900])
                else:
                    _reply_to_line(reply_token, str(answer)[:4900])
            except Exception as reply_exc:
                logger.warning("LINE_QA_REPLY_MESSAGE_SEND_FAILED: %s", reply_exc)
                fallback_message = "⚠️ エラーが発生しました（再試行してください）"
                if str(answer).strip() == fallback_message:
                    continue
                try:
                    if is_qa_answer_flow and immediate_sent and user_id:
                        _push_to_line_user(user_id, fallback_message)
                    else:
                        _reply_to_line(reply_token, fallback_message)
                except Exception as fallback_exc:
                    logger.warning("LINE_QA_REPLY_MESSAGE_FALLBACK_SEND_FAILED: %s", fallback_exc)


def _safe_process_callback_events(body: Dict[str, Any]) -> None:
    try:
        _process_callback_events(body)
    except Exception as exc:
        logger.warning("CALLBACK_PROCESSING_FAILED: %s", exc)


def handle_line_webhook(body: str, signature: str = "") -> Dict[str, Any]:
    try:
        payload = json.loads(body or "{}")
    except Exception as exc:
        logger.warning("CALLBACK_INVALID_JSON: %s", exc)
        return {"status": "ok"}

    events = payload.get("events", []) if isinstance(payload, dict) else []
    logger.info(
        "CALLBACK_RECEIVED: event_count=%s signature_present=%s",
        len(events),
        bool(str(signature or "").strip()),
    )
    threading.Thread(target=_safe_process_callback_events, args=(payload,), daemon=True).start()
    return {"status": "ok"}


@app.post("/callback")
async def callback(request: Request) -> Dict[str, Any]:
    try:
        body_text = await request.body()
        signature = request.headers.get("X-Line-Signature", "")
        return handle_line_webhook(body_text.decode("utf-8"), signature=signature)
    except Exception as exc:
        logger.warning("/callback failed but returns 200: %s", exc)
    return {"status": "ok"}


@app.post("/line-test")
async def line_test(body: dict = Body(...)) -> Response:
    question = str(body.get("question", "") or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required")

    minutes_markdown = _load_latest_minutes_markdown()
    answer = _generate_answer(question, minutes_markdown)
    return Response(answer, media_type="text/plain")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("line.line_webhook:app", host="0.0.0.0", port=8000, reload=True)
