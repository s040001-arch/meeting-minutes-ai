import os

import requests
from fastapi import FastAPI, Request

app = FastAPI()

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"

state = {"step": "idle", "answers": {}}


def handle_user_input(text: str) -> str:
    """LINEからのユーザー入力の入口。質問回答フローやOpenAI連携はここから接続する。"""
    # MVPではシングルユーザー前提で、状態はグローバルに保持する
    global state

    step = state.get("step", "idle")
    if step == "idle":
        state["step"] = "waiting_answer"
        if "answers" not in state or not isinstance(state.get("answers"), dict):
            state["answers"] = {}
        return "今日の会議の目的は何ですか？"

    if step == "waiting_answer":
        print(text)
        # 入力を後で参照できるように保存する（MVP: purposeのみ）
        state["answers"]["purpose"] = text
        state["step"] = "idle"
        return "ありがとうございます"

    # 想定外のstepの場合は安全側に戻す
    state["step"] = "idle"
    return "今日の会議の目的は何ですか？"


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
