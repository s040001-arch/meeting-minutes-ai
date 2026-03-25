import os

import requests
from fastapi import FastAPI, Request

app = FastAPI()

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"

ANSWERS_SAVE_PATH = os.path.join("data", "line_answers.json")

state = {
    # 質問送信は行わず、回答受付だけ行う前提のため
    # 「今は回答待ちの質問が存在する」状態から開始する。
    "pending_question": {
        "question_id": "purpose",
        "question_text": "今日の会議の目的は何ですか？",
    },
    "answers": {},
}

def save_answer_to_json(question_id: str, answer_text: str, question_text: str | None = None) -> None:
    """
    MVP用: webhookで受け取った回答をローカルJSONへ追記保存する。
    ここは低リスクの永続化で、失敗してもLINE応答は落とさない。
    """
    try:
        os.makedirs(os.path.dirname(ANSWERS_SAVE_PATH) or ".", exist_ok=True)
        record = {
            "question_id": question_id,
            "question_text": question_text,
            "answer_text": answer_text,
        }
        if os.path.exists(ANSWERS_SAVE_PATH):
            import json

            with open(ANSWERS_SAVE_PATH, "r", encoding="utf-8") as f:
                existing = json.load(f)
        else:
            existing = []

        if not isinstance(existing, list):
            existing = []

        existing.append(record)
        import json

        with open(ANSWERS_SAVE_PATH, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"save_answer_to_json_failed={e}")


def handle_user_input(text: str) -> str:
    """LINEからのユーザー入力の入口。pending question 1件に対する回答受付だけ行う。"""
    global state

    if "answers" not in state or not isinstance(state.get("answers"), dict):
        state["answers"] = {}

    pending = state.get("pending_question")
    if pending is None:
        return "現在、回答待ちの質問はありません。"
    pending_dict = pending if isinstance(pending, dict) else {}
    question_id = str(pending_dict.get("question_id") or "unknown")

    # 要件：少なくとも question_id と answer_text が確認できるログ
    state["answers"][question_id] = text
    print("unknown_answer_received=")
    print({"question_id": question_id, "answer_text": text})
    save_answer_to_json(
        question_id=question_id,
        answer_text=text,
        question_text=pending_dict.get("question_text"),
    )
    # 回答受領後は pending_question を None に統一（次の質問は決めない）
    state["pending_question"] = None

    return "ありがとうございます"


@app.get("/")
def health():
    return {"status": "ok"}


@app.post("/callback")
async def callback(request: Request):
    body = await request.json()
    print(body)

    try:
        events = body.get("events") or []
        if not events:
            return {"status": "ok"}

        evt = events[0]
        reply_token = evt.get("replyToken")
        message = evt.get("message") or {}

        if message.get("type") != "text":
            return {"status": "ok"}

        text = message.get("text")
        if not reply_token or text is None:
            return {"status": "ok"}

        reply_text = handle_user_input(text)

        channel_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
        if not channel_token:
            print("LINE_CHANNEL_ACCESS_TOKEN is not set")
            return {"status": "ok"}

        payload = {
            "replyToken": reply_token,
            "messages": [{"type": "text", "text": reply_text}],
        }
        resp = requests.post(
            LINE_REPLY_URL,
            headers={
                "Authorization": f"Bearer {channel_token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"LINE reply error: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"LINE reply exception: {e}")

    return {"status": "ok"}
