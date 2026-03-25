import os

import requests
from fastapi import FastAPI, Request

app = FastAPI()

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"

state = {
    # 質問送信は行わず、回答受付だけ行う前提のため
    # 「今は回答待ちの質問が存在する」状態から開始する。
    "pending_question": {
        "question_id": "purpose",
        "question_text": "今日の会議の目的は何ですか？",
    },
    "answers": {},
}


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
    # 回答受領後は pending_question を更新（statusは持たない）
    next_by_question_id = {
        "purpose": {
            "question_id": "participants",
            "question_text": "誰が参加しますか？",
        },
    }
    state["pending_question"] = next_by_question_id.get(question_id)

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
