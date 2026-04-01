import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

import requests

from repo_env import load_dotenv_local

load_dotenv_local()
try:
    from railway_bootstrap import write_google_oauth_files_from_env

    write_google_oauth_files_from_env()
except Exception:
    pass
from fastapi import FastAPI, HTTPException, Request
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from googleapiclient.discovery import build

from progress_tracker import read_job_progress, read_last_job_progress

app = FastAPI()

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"

# MVP検証用: ローカルJSON。PaaS（例: Railway）のエフェメラルFSでは再起動で消える。次段階でDB/Sheets等へ差し替え前提。
ANSWERS_SAVE_PATH = os.path.join("data", "line_answers.json")
SHEETS_ID = os.getenv("LINE_ANSWERS_SHEETS_ID", "").strip()
SHEETS_TAB_NAME = os.getenv("LINE_ANSWERS_SHEETS_TAB", "answers").strip() or "answers"
SERVICE_ACCOUNT_JSON_PATH = os.getenv(
    "GOOGLE_SERVICE_ACCOUNT_JSON", "credentials_service_account.json"
).strip()

AUTO_AFTER_ANSWER_ENV = "AUTO_AFTER_ANSWER"
LOCKS_DIR = os.path.join("data", "locks")
# stale "starting" lock older than this may be removed (crash before Popen)
_AUTO_AFTER_ANSWER_STARTING_STALE_SEC = 120.0

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
_RUN_DOCS_HUB_E2E = os.path.join(_REPO_ROOT, "run_docs_hub_e2e.py")

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


def _env_auto_after_answer_enabled() -> bool:
    v = os.getenv(AUTO_AFTER_ANSWER_ENV, "").strip().lower()
    if not v:
        return False
    return v in ("1", "true", "yes", "on")


def _auto_after_answer_lock_path(job_id: str) -> str:
    h = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:16]
    return os.path.join(LOCKS_DIR, f"auto_after_answer_{h}.lock")


def _read_lock_lines(path: str) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return [ln.strip() for ln in f.read().splitlines()]
    except OSError:
        return []


def _lock_mtime_age_sec(path: str) -> float:
    try:
        return max(0.0, time.time() - os.path.getmtime(path))
    except OSError:
        return 0.0


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _try_acquire_auto_after_answer_lock(job_id: str) -> str | None:
    """
    Exclusive lock file for this job. Returns path if acquired, else None.
    Stale locks (dead pid or old 'starting') are removed and retried.
    """
    os.makedirs(LOCKS_DIR, exist_ok=True)
    path = _auto_after_answer_lock_path(job_id)
    for _ in range(4):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, b"starting\n")
                os.fsync(fd)
            finally:
                os.close(fd)
            return path
        except FileExistsError:
            lines = _read_lock_lines(path)
            first = (lines[0] if lines else "").strip()
            age = _lock_mtime_age_sec(path)
            if first == "starting":
                if age > _AUTO_AFTER_ANSWER_STARTING_STALE_SEC:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                    continue
                return None
            try:
                pid = int(first) if first.isdigit() else -1
            except ValueError:
                pid = -1
            if pid > 0 and _is_pid_alive(pid):
                return None
            try:
                os.remove(path)
            except OSError:
                pass
    return None


def _write_lock_child_pid(path: str, child_pid: int, job_id: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"{child_pid}\n{job_id}\n")


def _release_lock_path(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _wait_child_and_release_lock(proc: subprocess.Popen, lock_path: str) -> None:
    try:
        proc.wait()
    finally:
        _release_lock_path(lock_path)


def maybe_launch_auto_after_answer(job_id: str | None, save_ok: bool) -> None:
    enabled = _env_auto_after_answer_enabled()
    jid = str(job_id).strip() if job_id else ""
    print(
        f"auto_after_answer_enabled={enabled} job_id={jid!r} save_ok={save_ok}"
    )
    if not enabled:
        print("auto_after_answer_skipped reason=flag_off")
        return
    if not save_ok:
        print("auto_after_answer_skipped reason=save_failed")
        return
    if not jid:
        print("auto_after_answer_skipped reason=job_id_missing")
        return

    lock_path = _try_acquire_auto_after_answer_lock(jid)
    if lock_path is None:
        print(f"auto_after_answer_skipped reason=lock_exists job_id={jid!r}")
        return

    cmd = [
        sys.executable,
        _RUN_DOCS_HUB_E2E,
        "--job-id",
        jid,
        "--after-answer",
        "--push",
    ]
    line_user_id = os.getenv("LINE_USER_ID", "").strip()
    line_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if line_user_id and line_token:
        cmd.append("--send-line")
    print(
        "auto_after_answer_cmd="
        f"{sys.executable} run_docs_hub_e2e.py "
        f"--job-id {jid!r} --after-answer --push "
        f"{'--send-line ' if ('--send-line' in cmd) else ''}cwd={_REPO_ROOT!r}"
    )

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=_REPO_ROOT,
            close_fds=True,
        )
    except Exception as e:
        print(f"auto_after_answer_skipped reason=launch_error err={e!r}")
        _release_lock_path(lock_path)
        return

    _write_lock_child_pid(lock_path, proc.pid, jid)
    threading.Thread(
        target=_wait_child_and_release_lock,
        args=(proc, lock_path),
        daemon=True,
    ).start()
    print(f"auto_after_answer_launched job_id={jid!r} pid={proc.pid}")


def save_answer_to_json(
    question_id: str,
    answer_text: str,
    question_text: str | None = None,
    user_id: str | None = None,
    job_id: str | None = None,
) -> bool:
    """
    MVP検証用: webhookで受け取った回答をローカルJSONへ追記（本番永続ストレージではない）。
    失敗してもLINE応答は落とさない。
    """
    try:
        os.makedirs(os.path.dirname(ANSWERS_SAVE_PATH) or ".", exist_ok=True)
        job_id_str = "" if job_id is None else str(job_id).strip()
        record = {
            "received_at": now_iso(),
            "question_id": question_id,
            "question_text": question_text,
            "answer_text": answer_text,
            "user_id": user_id,
            "job_id": job_id_str,
        }
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
        return True
    except Exception as e:
        print(f"save_answer_to_json_failed={e}")
        return False


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
) -> bool:
    ok = save_answer_to_json(
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
    return ok


def _unknown_points_path(job_id: str | None) -> str | None:
    jid = str(job_id or "").strip()
    if not jid:
        return None
    return os.path.join("data", "transcriptions", jid, "unknown_points.json")


def _load_unknown_points_for_job(job_id: str | None) -> list[dict]:
    path = _unknown_points_path(job_id)
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []
    except Exception as e:
        print(f"load_unknown_points_for_job_failed={e}")
        return []


def _save_unknown_points_for_job(job_id: str | None, unknown_points: list[dict]) -> None:
    path = _unknown_points_path(job_id)
    if not path:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(unknown_points, f, ensure_ascii=False, indent=2)


def _mark_unknown_points_answered(
    *,
    job_id: str | None,
    selected_unknown: dict | None,
    answer_text: str,
    question_id: str,
) -> int:
    if not job_id or not isinstance(selected_unknown, dict):
        return 0
    target_text = str(selected_unknown.get("text") or "").strip()
    target_type = str(selected_unknown.get("type") or "").strip()
    if not target_text:
        return 0
    unknown_points = _load_unknown_points_for_job(job_id)
    if not unknown_points:
        return 0

    updated_count = 0
    for item in unknown_points:
        if str(item.get("status", "")).strip().lower() == "answered":
            continue
        item_text = str(item.get("text") or "").strip()
        item_type = str(item.get("type") or "").strip()
        if item_text != target_text:
            continue
        if target_type and item_type and item_type != target_type:
            continue
        item["status"] = "answered"
        item["answer"] = answer_text
        item["answered_by_question_id"] = question_id
        item["answered_at"] = now_iso()
        updated_count += 1

    if updated_count > 0:
        _save_unknown_points_for_job(job_id, unknown_points)
    return updated_count


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
        selected_unknown = ctx.get("selected_unknown")
        if not isinstance(selected_unknown, dict):
            selected_unknown = None
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
        selected_unknown = pending_file.get("selected_unknown")
        if not isinstance(selected_unknown, dict):
            selected_unknown = None
    else:
        return "現在、回答待ちの質問はありません。"

    # 要件：少なくとも question_id と answer_text が確認できるログ
    state["answers"][question_id] = text
    print("unknown_answer_received=")
    print({"question_id": question_id, "answer_text": text, "job_id": job_id_for_save})
    save_ok = persist_answer(
        question_id=question_id,
        answer_text=text,
        question_text=qtext_for_save,
        user_id=user_id,
        job_id=job_id_for_save,
    )
    answered_updates = _mark_unknown_points_answered(
        job_id=job_id_for_save,
        selected_unknown=selected_unknown,
        answer_text=text,
        question_id=question_id,
    )
    print(
        "unknown_points_answered_update="
        f"job_id={job_id_for_save!r} updated_count={answered_updates}"
    )
    try:
        maybe_launch_auto_after_answer(job_id_for_save, save_ok)
    except Exception as e:
        print(f"auto_after_answer_exception={e!r}")
    state["pending_question"] = None
    state["remote_pending"] = None

    return "ありがとうございます"


def _maybe_launch_drive_worker_from_app_startup() -> None:
    """
    Railway/起動経路が ENTRYPOINT/CMD を通らないケースでも Drive 常駐ワーカーを起動するため。

    drive_auto_run_forever.py 側にロックがあるため、既に動いていれば二重起動は抑制される。
    """

    drive_folder_id = os.getenv("DRIVE_FOLDER_ID", "").strip()
    if not drive_folder_id:
        return

    # railway_entry.sh 相当の環境変数を可能な限り引き継ぐ
    interval_sec = int(os.getenv("DRIVE_POLL_INTERVAL_SEC", "120").strip() or "120")

    credentials_path = os.getenv("GOOGLE_DRIVE_CREDENTIALS_PATH", "credentials.json").strip()
    token_path = os.getenv("GOOGLE_DRIVE_TOKEN_PATH", "token_drive.json").strip()
    state_path = os.getenv("DRIVE_STATE_PATH", os.path.join("data", "last_seen_file_ids.json")).strip()
    download_dir = os.getenv("DRIVE_DOWNLOAD_DIR", os.path.join("data", "incoming_audio")).strip()

    lock_hint = os.path.join("logs", "drive_auto_run_forever.lock")
    print(
        "[drive_worker_bootstrap] starting (or skipping via lock) "
        f"DRIVE_FOLDER_ID={drive_folder_id!r} interval_sec={interval_sec} lock_hint={lock_hint!r}"
    )

    cmd = [
        sys.executable,
        os.path.join(_REPO_ROOT, "drive_auto_run_forever.py"),
        "--folder-id",
        drive_folder_id,
        "--credentials",
        credentials_path,
        "--token",
        token_path,
        "--state",
        state_path,
        "--download-dir",
        download_dir,
        "--update-state",
        "--interval-sec",
        str(interval_sec),
    ]

    # stdout/stderr は親プロセスに継承させ、Deploy Logs に出るようにする
    subprocess.Popen(cmd, cwd=_REPO_ROOT, close_fds=True)


@app.on_event("startup")
def _on_app_startup() -> None:
    try:
        _maybe_launch_drive_worker_from_app_startup()
    except Exception as e:
        # 起動失敗して webhook が落ちるのは避ける
        print(f"[drive_worker_bootstrap] failed err={e!r}")


@app.get("/")
def health():
    return {"status": "ok"}


@app.get("/job-progress")
def job_progress(job_id: str | None = None):
    """
    進捗の常時参照用。
    - job_id 無指定: data/last_job_progress.json を返す
    - job_id 指定: data/transcriptions/{job_id}/progress.json を返す
    """
    input_root = os.getenv("PROGRESS_INPUT_ROOT", "data/transcriptions").strip() or "data/transcriptions"
    if job_id:
        data = read_job_progress(input_root=input_root, job_id=job_id)
        if not data:
            raise HTTPException(status_code=404, detail="job progress not found")
        return data

    data = read_last_job_progress()
    if not data:
        raise HTTPException(status_code=404, detail="last job progress not found")
    return data


@app.get("/worker-status")
def worker_status():
    """
    Drive worker状態の常時参照用。
    - polling / processing / idle / error
    - last_result: success / no_new_files / failed 等
    """
    path = os.getenv("WORKER_STATUS_PATH", os.path.join("data", "worker_status.json")).strip()
    if not path:
        path = os.path.join("data", "worker_status.json")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="worker status not found")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"worker status read failed: {e}") from e
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="worker status is invalid")
    return data


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
