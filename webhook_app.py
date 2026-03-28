import os
import json
from datetime import datetime

import requests

from repo_env import load_dotenv_local

load_dotenv_local()
from fastapi import FastAPI, HTTPException, Request
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from googleapiclient.discovery import build

app = FastAPI()

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"

# MVP検証用: ローカルJSON。PaaS（例: Railway）のエフェメラルFSでは再起動で消える。次段階でDB/Sheets等へ差し替え前提。
ANSWERS_SAVE_PATH = os.path.join("data", "line_answers.json")
SHEETS_ID = os.getenv("LINE_ANSWERS_SHEETS_ID", "").strip()
SHEETS_TAB_NAME = os.getenv("LINE_ANSWERS_SHEETS_TAB", "answers").strip() or "answers"
SERVICE_ACCOUNT_JSON_PATH = os.getenv(
    "GOOGLE_SERVICE_ACCOUNT_JSON", "credentials_service_account.json"
).strip()

state = {
    # 質問送信は行わず、回答受付だけ行う前提のため
    # 「今は回答待ちの質問が存在する」状態から開始する。
    "pending_question": {
        "question_id": "purpose",
        "question_text": "今日の会議の目的は何ですか？",
    },
    "answers": {},
}

def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def save_answer_to_json(
    question_id: str,
    answer_text: str,
    question_text: str | None = None,
    user_id: str | None = None,
    job_id: str | None = None,
) -> None:
    """
    MVP検証用: webhookで受け取った回答をローカルJSONへ追記（本番永続ストレージではない）。
    失敗してもLINE応答は落とさない。
    """
    try:
        os.makedirs(os.path.dirname(ANSWERS_SAVE_PATH) or ".", exist_ok=True)
        record = {
            "received_at": now_iso(),
            "question_id": question_id,
            "question_text": question_text,
            "answer_text": answer_text,
            "user_id": user_id,
        }
        if job_id:
            record["job_id"] = job_id
        if os.path.exists(ANSWERS_SAVE_PATH):
            with open(ANSWERS_SAVE_PATH, "r", encoding="utf-8") as f:
                existing = json.load(f)
        else:
            existing = []

        if not isinstance(existing, list):
            existing = []

        existing.append(record)

        with open(ANSWERS_SAVE_PATH, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"save_answer_to_json_failed={e}")


def save_answer_to_google_sheet(
    question_id: str,
    answer_text: str,
    question_text: str | None = None,
    user_id: str | None = None,
) -> None:
    """
    Task 7-1/7-2 スライス:
    環境変数が設定されている場合のみ、回答をGoogle Sheetsへ追記する。
    失敗しても webhook 応答は継続する。
    """
    if not SHEETS_ID:
        return
    if not os.path.isfile(SERVICE_ACCOUNT_JSON_PATH):
        print(
            "save_answer_to_google_sheet_skipped="
            f"service_account_json_not_found:{SERVICE_ACCOUNT_JSON_PATH}"
        )
        return
    try:
        creds = ServiceAccountCredentials.from_service_account_file(
            SERVICE_ACCOUNT_JSON_PATH,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        service = build("sheets", "v4", credentials=creds)
        values = [
            [
                now_iso(),
                question_id,
                question_text or "",
                answer_text,
                user_id or "",
            ]
        ]
        service.spreadsheets().values().append(
            spreadsheetId=SHEETS_ID,
            range=f"{SHEETS_TAB_NAME}!A:E",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": values},
        ).execute()
    except Exception as e:
        print(f"save_answer_to_google_sheet_failed={e}")


def load_line_pending_context() -> dict:
    path = os.path.join("data", "line_pending_context.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"load_line_pending_context_failed={e}")
        return {}


def get_pending_context_from_pipeline() -> dict:
    """
    優先: パイプラインから HTTP で同期された remote_pending（Railway 用）。
    次: 同一マシン上の line_pending_context.json（ローカル一体運用）。
    """
    r = state.get("remote_pending")
    if isinstance(r, dict) and r.get("job_id"):
        return r
    f = load_line_pending_context()
    if isinstance(f, dict) and f.get("job_id"):
        return f
    return {}


def persist_answer(
    question_id: str,
    answer_text: str,
    question_text: str | None = None,
    user_id: str | None = None,
    job_id: str | None = None,
) -> None:
    save_answer_to_json(
        question_id=question_id,
        answer_text=answer_text,
        question_text=question_text,
        user_id=user_id,
        job_id=job_id,
    )
    save_answer_to_google_sheet(
        question_id=question_id,
        answer_text=answer_text,
        question_text=question_text,
        user_id=user_id,
    )


def handle_user_input(text: str, user_id: str | None = None) -> str:
    """LINEからのユーザー入力の入口。pending question 1件に対する回答受付だけ行う。"""
    global state

    if "answers" not in state or not isinstance(state.get("answers"), dict):
        state["answers"] = {}

    ctx = get_pending_context_from_pipeline()
    pending = state.get("pending_question")

    if ctx.get("job_id"):
        question_id = str(ctx.get("question_id") or "unknown")
        qtext_for_save = str(ctx.get("question_text") or "").strip()
        job_id_for_save = str(ctx.get("job_id") or "").strip() or None
    elif pending is not None:
        pending_dict = pending if isinstance(pending, dict) else {}
        question_id = str(pending_dict.get("question_id") or "unknown")
        pending_file = load_line_pending_context()
        if pending_file.get("question_id"):
            question_id = str(pending_file.get("question_id"))
        qtext_for_save = pending_dict.get("question_text")
        if isinstance(pending_file.get("question_text"), str) and pending_file["question_text"].strip():
            qtext_for_save = pending_file["question_text"].strip()
        job_id_for_save = str(pending_file.get("job_id") or "").strip() or None
    else:
        return "現在、回答待ちの質問はありません。"

    # 要件：少なくとも question_id と answer_text が確認できるログ
    state["answers"][question_id] = text
    print("unknown_answer_received=")
    print({"question_id": question_id, "answer_text": text, "job_id": job_id_for_save})
    persist_answer(
        question_id=question_id,
        answer_text=text,
        question_text=qtext_for_save,
        user_id=user_id,
        job_id=job_id_for_save,
    )
    state["pending_question"] = None
    state["remote_pending"] = None

    return "ありがとうございます"


@app.get("/")
def health():
    return {"status": "ok"}


@app.post("/line/pending")
async def line_pending_sync(request: Request):
    """
    ローカルで run_question_cycle が LINE push した直後に POST される。
    Railway 上では data/line_pending_context.json が無いため、ここで remote_pending を更新する。
    """
    secret = os.getenv("LINE_PENDING_SYNC_SECRET", "").strip()
    auth = request.headers.get("Authorization") or ""
    if secret:
        if auth != f"Bearer {secret}":
            raise HTTPException(status_code=401, detail="unauthorized")
    body = await request.json()
    if not isinstance(body, dict) or not body.get("job_id"):
        raise HTTPException(status_code=400, detail="body must include job_id")
    state["remote_pending"] = body
    print(f"line_pending_synced job_id={body.get('job_id')}")
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
        user_id = str((evt.get("source") or {}).get("userId") or "")
        if not reply_token or text is None:
            return {"status": "ok"}

        reply_text = handle_user_input(text, user_id=user_id or None)

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
