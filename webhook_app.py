import os

import requests
from fastapi import FastAPI, Request

app = FastAPI()

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"

_user_state: dict[str, dict] = {}


def handle_user_input(text: str, user_id: str) -> None:
    """LINEからのユーザー入力の入口。質問回答フローやOpenAI連携はここから接続する。"""
    # まずはメモリ上でユーザー状態を保持する（MVP: process内限定）
    if user_id not in _user_state:
        _user_state[user_id] = {"step": "idle"}

    # ここでは最小構成のため状態遷移はまだ実装しない
    _ = text
    _ = _user_state[user_id]


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

        print(text)
        source = evt.get("source") or {}
        user_id = source.get("userId")
        if not user_id:
            user_id = "unknown"
        handle_user_input(text, user_id)

        channel_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
        if not channel_token:
            print("LINE_CHANNEL_ACCESS_TOKEN is not set")
            return {"status": "ok"}

        payload = {
            "replyToken": reply_token,
            "messages": [{"type": "text", "text": text}],
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
