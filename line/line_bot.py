import json
from pathlib import Path
from typing import Any, Dict, Optional

from openai import OpenAI

from config import settings
from utils.logger import logger

LATEST_MEETING_FILE = Path(
    getattr(settings, "LATEST_MEETING_STATE_FILE", "latest_meeting.json")
)
OPENAI_MODEL = getattr(settings, "LINE_QA_MODEL", "gpt-4.1-mini")

client = OpenAI(api_key=settings.OPENAI_API_KEY)


def load_latest_meeting_state() -> Optional[Dict[str, Any]]:
    try:
        if not LATEST_MEETING_FILE.exists():
            logger.warning(f"latest meeting state file not found: {LATEST_MEETING_FILE}")
            return None

        data = json.loads(LATEST_MEETING_FILE.read_text(encoding="utf-8"))

        if not isinstance(data, dict):
            logger.warning("latest_meeting.json is not a dict")
            return None

        minutes_text = str(data.get("minutes_text") or "").strip()
        if not minutes_text:
            logger.warning("latest_meeting.json missing minutes_text")
            return None

        return data
    except Exception as exc:
        logger.warning(f"failed to load latest_meeting.json: {exc}")
        return None


def _build_meeting_info_text(meeting_info: Any) -> str:
    if not isinstance(meeting_info, dict):
        return "不明"

    year = str(meeting_info.get("year", "")).strip()
    month_day = str(meeting_info.get("month_day", "")).strip()
    customer_name = str(meeting_info.get("customer_name", "")).strip()
    meeting_title = str(meeting_info.get("meeting_title", "")).strip()

    parts = [
        f"年: {year or '不明'}",
        f"日付: {month_day or '不明'}",
        f"顧客名: {customer_name or '不明'}",
        f"会議タイトル: {meeting_title or '不明'}",
    ]
    return "
".join(parts)


def _build_prompt(question: str, latest_state: Dict[str, Any]) -> str:
    google_docs_url = str(latest_state.get("google_docs_url") or "").strip()
    meeting_info_text = _build_meeting_info_text(latest_state.get("meeting_info"))
    minutes_text = str(latest_state.get("minutes_text") or "").strip()

    return f"""あなたはLINE上で議事録Q&Aを行うアシスタントです。
必ず「最後に生成された最新の議事録 1 件だけ」を参照して、ユーザーの自由文質問に1問1答で答えてください。

制約:
- 参照してよいのは以下の最新議事録情報のみ
- 他の会議や過去議事録を推測しない
- 議事録に書かれていないことは、書かれていないと明示する
- 回答は日本語で簡潔にする
- 必要なら Google Docs URL を最後に1行だけ添える
- 箇条書きは最小限にする

最新議事録 Google Docs URL:
{google_docs_url or 'なし'}

会議情報:
{meeting_info_text}

最新議事録本文:
{minutes_text}

ユーザー質問:
{question}
"""


def _call_openai(prompt: str) -> str:
    response = client.responses.create(
        model=OPENAI_MODEL,
        input=prompt,
    )

    output_text = getattr(response, "output_text", "") or ""
    answer = str(output_text).strip()
    if answer:
        return answer

    return "議事録の回答生成に失敗しました。"


def answer_line_question(message_text: str) -> str:
    question = str(message_text or "").strip()
    if not question:
        return "質問を入力してください。"

    latest_state = load_latest_meeting_state()
    if not latest_state:
        return "まだ最新議事録を参照できません"

    minutes_text = str(latest_state.get("minutes_text") or "").strip()
    if not minutes_text:
        return "まだ最新議事録を参照できません"

    try:
        prompt = _build_prompt(question, latest_state)
        answer = _call_openai(prompt).strip()
        return answer or "まだ最新議事録を参照できません"
    except Exception as exc:
        logger.warning(f"LINE Q&A failed: {exc}")
        return "まだ最新議事録を参照できません"


def handle_line_text_message(message_text: str) -> str:
    return answer_line_question(message_text)
