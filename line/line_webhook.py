import json
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
    save_session,
)
from utils.logger import get_logger

logger = get_logger(__name__)

app = FastAPI()

_LINE_REPLY_ENDPOINT = "https://api.line.me/v2/bot/message/reply"
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
- 元議事録の構成を維持する
- 回答内容のみを反映して不足情報を補完する
- 推測で新事実を追加しない
- 出力はMarkdown本文のみ

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
    docs_result = write_minutes_to_google_docs(
        meeting_info=meeting_info,
        minutes_text=updated_markdown,
    )
    docs_url = str(
        docs_result.get("document_url")
        or docs_result.get("google_docs_url")
        or session.get("docs_url")
        or ""
    ).strip()

    _save_latest_minutes_markdown(updated_markdown, metadata)

    session["docs_url"] = docs_url
    session["status"] = "completed"
    save_session(session)
    logger.info(
        "LINE_QA_DOCS_UPDATED: meeting_id=%s docs_url=%s",
        str(session.get("meeting_id") or session.get("drive_folder_id") or ""),
        docs_url,
    )
    return docs_url


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
        return ""

    logger.info(
        "LINE_QA_ANSWER_RECEIVED: meeting_id=%s current_index=%s answer=%s",
        str(session.get("meeting_id") or session.get("drive_folder_id") or ""),
        int(session.get("current_question_index") or 0),
        question_answer,
    )
    updated = append_answer_and_advance(session, question_answer)
    next_question = get_current_question(updated)
    total = len(updated.get("ambiguity_questions") or [])
    current_index = int(updated.get("current_question_index") or 0)

    if next_question:
        logger.info(
            "LINE_QA_NEXT_QUESTION: next_index=%s total=%s question=%s",
            current_index,
            total,
            next_question,
        )
        return (
            f"【確認 {current_index + 1}/{total}】\n"
            f"{next_question}\n"
            "分からなければ『スキップ』と入力してください"
        )

    docs_url = _apply_answers_and_update_docs(updated)
    logger.info("LINE_QA_FLOW_COMPLETED: docs_url=%s", docs_url)
    return (
        "確認回答ありがとうございます。\n"
        "回答を反映して議事録を更新しました。\n"
        f"docs_url: {docs_url}"
    )


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

    with urlrequest.urlopen(req) as resp:
        if resp.status >= 400:
            raise HTTPException(status_code=500, detail="Failed to reply to LINE")


@app.post("/callback")
async def callback(request: Request) -> Dict[str, Any]:
    try:
        print(await request.json())
        body = await request.json()
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

            try:
                active_session = _find_target_active_session()
                if active_session is not None:
                    answer = _handle_ambiguity_answer(question)
                else:
                    answer = _generate_answer(question, minutes_markdown)
            except Exception as exc:
                logger.warning("OpenAI answer generation failed: %s", exc)
                answer = "回答生成に失敗しました。時間をおいて再度お試しください。"

            _reply_to_line(reply_token, answer)
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
