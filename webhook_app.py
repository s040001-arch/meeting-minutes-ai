import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime

import anthropic
import requests

from repo_env import load_dotenv_local

load_dotenv_local()
try:
    from railway_bootstrap import write_google_oauth_files_from_env

    write_google_oauth_files_from_env()
except Exception:
    pass
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from googleapiclient.discovery import build


from progress_tracker import read_job_progress, read_last_job_progress, update_job_progress

app = FastAPI()

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"

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
_RUN_RESUME_FROM_STEP7 = os.path.join(_REPO_ROOT, "run_resume_from_step7.py")
CORRECTION_DICT_PATH = os.path.join("data", "correction_dict.json")
LINE_EXTRACTOR_MODEL = "claude-sonnet-4-20250514"

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


def _job_visible_log_path(job_id: str | None) -> str | None:
    jid = str(job_id or "").strip()
    if not jid:
        return None
    return os.path.join("data", "transcriptions", jid, "processing_visible_log.txt")


ANSWERS_JSON_FILENAME = "answers.json"


def _answers_json_path(job_id: str | None) -> str | None:
    jid = str(job_id or "").strip()
    if not jid:
        return None
    return os.path.join("data", "transcriptions", jid, ANSWERS_JSON_FILENAME)


def _append_to_answers_json(
    job_id: str | None,
    *,
    question_id: str,
    question_text: str,
    answer_text: str,
) -> None:
    """回答内容を answers.json に追記する。Step⑰ ナレッジ蓄積の入力として使用する。"""
    path = _answers_json_path(job_id)
    if not path:
        return
    existing: list[dict] = []
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                existing = [x for x in data if isinstance(x, dict)]
        except Exception:
            pass
    existing.append({
        "question_id": question_id,
        "question_text": question_text,
        "answer_text": answer_text,
        "answered_at": now_iso(),
    })
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f"append_to_answers_json_failed={exc!r}")


def _record_job_visible_log(job_id: str | None, message: str) -> None:
    ts = now_iso()
    line = f"[{ts}] {message}"
    print(line)
    path = _job_visible_log_path(job_id)
    if path:
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            print(f"job_visible_log_write_failed={e!r}")
    if job_id:
        try:
            from run_job_once import append_log_to_drive

            append_log_to_drive(str(job_id), message)
        except Exception as e:
            print(f"job_visible_log_drive_append_failed={e!r}")


def _update_job_phase(
    *,
    job_id: str | None,
    phase: str,
    status: str,
    detail: dict | None = None,
    overall_status: str | None = None,
) -> None:
    jid = str(job_id or "").strip()
    if not jid:
        return
    try:
        update_job_progress(
            input_root="data/transcriptions",
            job_id=jid,
            phase=phase,
            status=status,
            detail=detail or {},
            overall_status=overall_status,
        )
    except Exception as e:
        print(f"update_job_phase_failed={e!r}")


def _extract_json_object(text: str) -> dict:
    s = (text or "").strip()
    start = s.find("{")
    end = s.rfind("}")
    if start < 0 or end < start:
        raise ValueError("json object not found in text")
    payload = json.loads(s[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("parsed JSON is not an object")
    return payload


def _extract_output_text_from_anthropic(resp) -> str:
    texts: list[str] = []
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", "") == "text":
            texts.append(str(getattr(block, "text", "") or ""))
    return "\n".join(t for t in texts if t).strip()


def _resolve_active_job_id_fallback() -> str | None:
    ctx = get_pending_context_from_pipeline()
    jid = str(ctx.get("job_id") or "").strip()
    if jid:
        return jid
    latest = read_last_job_progress() or {}
    jid = str(latest.get("job_id") or "").strip()
    return jid or None


def _heuristic_extract_correction_pairs(text: str) -> list[dict[str, str]]:
    patterns = [
        r"(?P<wrong>.+?)じゃなくて(?P<correct>.+)",
        r"(?P<wrong>.+?)ではなく(?P<correct>.+)",
        r"(?P<wrong>.+?)の間違いで(?P<correct>.+)",
        r"(?P<wrong>.+?)(?:→|->|=>)(?P<correct>.+)",
        r"(?P<wrong>.+?)は(?P<correct>.+?)です",
    ]
    pairs: list[dict[str, str]] = []
    for pattern in patterns:
        m = re.search(pattern, text)
        if not m:
            continue
        wrong = str(m.group("wrong") or "").strip(" 「」\"'。.,、")
        correct = str(m.group("correct") or "").strip(" 「」\"'。.,、")
        if wrong and correct and wrong != correct:
            pairs.append({"wrong": wrong, "correct": correct})
            break
    return pairs


def _normalize_correction_pairs(raw_pairs: object) -> list[dict[str, str]]:
    pairs: list[dict[str, str]] = []
    if not isinstance(raw_pairs, list):
        return pairs
    for item in raw_pairs:
        if not isinstance(item, dict):
            continue
        wrong = str(item.get("wrong") or "").strip()
        correct = str(item.get("correct") or "").strip()
        if wrong and correct and wrong != correct:
            pairs.append({"wrong": wrong, "correct": correct})
    return pairs


def _looks_noninformative_message(text: str) -> bool:
    s = re.sub(r"\s+", "", str(text or "").strip()).lower()
    if not s:
        return True
    bland = {
        "了解",
        "りょうかい",
        "ok",
        "okay",
        "yes",
        "ありがとうございます",
        "ありがとう",
        "承知しました",
        "承知です",
        "確認します",
        "確認します。",
    }
    return s in {x.lower() for x in bland}


def _extract_line_message_actions_heuristic(text: str, pending_context: dict | None) -> dict:
    pairs = _heuristic_extract_correction_pairs(text)
    has_pending_question = bool(
        pending_context and str((pending_context or {}).get("question_text") or "").strip()
    )
    informative = not _looks_noninformative_message(text)
    has_answer = has_pending_question and informative
    return {
        "is_irrelevant": (not has_answer and not pairs),
        "has_answer": has_answer,
        "answer_text": str(text or "").strip() if has_answer else "",
        "correction_pairs": pairs,
        "reason": (
            "heuristic_answer_and_correction"
            if has_answer and pairs
            else "heuristic_answer"
            if has_answer
            else "heuristic_correction"
            if pairs
            else "heuristic_irrelevant"
        ),
    }


def _extract_line_message_actions_with_claude(text: str, pending_context: dict | None) -> dict:
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return _extract_line_message_actions_heuristic(text, pending_context)
    client = anthropic.Anthropic(api_key=api_key)
    compact_ctx = {
        "has_pending_question": bool(
            pending_context and str((pending_context or {}).get("question_text") or "").strip()
        ),
        "job_id": str((pending_context or {}).get("job_id") or "").strip(),
        "question_id": str((pending_context or {}).get("question_id") or "").strip(),
        "question_text": str((pending_context or {}).get("question_text") or "").strip(),
        "selected_unknown": (pending_context or {}).get("selected_unknown"),
        "message_text": text,
    }
    system_prompt = (
        "あなたは議事録補正システムのLINEメッセージ情報抽出器です。"
        "目的は、受信メッセージから議事録補正に使える情報をできるだけ回収することです。"
        "『回答か修正依頼か』を1つに決めるのではなく、両方あれば両方を返してください。"
        "has_answer は、直前の質問に対する答えとして本文へ反映できる情報があるかを表します。"
        "answer_text には、本文反映に使う部分だけを自然な日本語で短く入れてください。"
        "correction_pairs には、『XではなくY』『Xの間違い』のような置換ペアを入れてください。"
        "雑談や挨拶だけで、議事録補正に使える情報がない場合のみ is_irrelevant=true にしてください。"
        "出力は必ずJSONオブジェクトのみ。"
        '形式は {"is_irrelevant":true|false,"has_answer":true|false,"answer_text":"string","correction_pairs":[{"wrong":"...","correct":"..."}],"reason":"string"} としてください。'
    )
    try:
        resp = client.messages.create(
            model=LINE_EXTRACTOR_MODEL,
            max_tokens=800,
            temperature=0,
            system=system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": json.dumps(compact_ctx, ensure_ascii=False),
                }
            ],
        )
        parsed = _extract_json_object(_extract_output_text_from_anthropic(resp))
        has_answer = bool(parsed.get("has_answer"))
        answer_text = str(parsed.get("answer_text") or "").strip()
        pairs = _normalize_correction_pairs(parsed.get("correction_pairs"))
        is_irrelevant = bool(parsed.get("is_irrelevant"))
        if has_answer and not answer_text:
            answer_text = str(text or "").strip()
        if has_answer:
            is_irrelevant = False
        if pairs:
            is_irrelevant = False
        return {
            "is_irrelevant": is_irrelevant,
            "has_answer": has_answer,
            "answer_text": answer_text,
            "correction_pairs": pairs,
            "reason": str(parsed.get("reason") or "").strip(),
        }
    except Exception as e:
        print(f"line_message_extraction_fallback={e!r}")
        fallback = _extract_line_message_actions_heuristic(text, pending_context)
        fallback["reason"] = f"heuristic_fallback:{fallback.get('reason')}"
        return fallback


def _load_correction_dict() -> dict[str, str]:
    if not os.path.isfile(CORRECTION_DICT_PATH):
        return {}
    try:
        with open(CORRECTION_DICT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return {
            str(k).strip(): str(v).strip()
            for k, v in data.items()
            if str(k).strip() and str(v).strip()
        }
    except Exception as e:
        print(f"load_correction_dict_failed={e!r}")
        return {}


def _save_correction_dict(data: dict[str, str]) -> None:
    os.makedirs(os.path.dirname(CORRECTION_DICT_PATH) or ".", exist_ok=True)
    with open(CORRECTION_DICT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _merge_correction_pairs(pairs: list[dict[str, str]]) -> tuple[int, int]:
    current = _load_correction_dict()
    added = 0
    updated = 0
    for item in pairs:
        wrong = str(item.get("wrong") or "").strip()
        correct = str(item.get("correct") or "").strip()
        if not wrong or not correct or wrong == correct:
            continue
        if wrong not in current:
            added += 1
        elif current[wrong] != correct:
            updated += 1
        current[wrong] = correct
    _save_correction_dict(current)
    return added, updated

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


def _launch_resume_subprocess(
    *,
    job_id: str | None,
    save_ok: bool,
    cmd: list[str],
    launch_label: str,
) -> None:
    enabled = _env_auto_after_answer_enabled()
    jid = str(job_id).strip() if job_id else ""
    print(
        f"{launch_label}_enabled={enabled} job_id={jid!r} save_ok={save_ok}"
    )
    if not enabled:
        print(f"{launch_label}_skipped reason=flag_off")
        return
    if not save_ok:
        print(f"{launch_label}_skipped reason=save_failed")
        return
    if not jid:
        print(f"{launch_label}_skipped reason=job_id_missing")
        return

    lock_path = _try_acquire_auto_after_answer_lock(jid)
    if lock_path is None:
        print(f"{launch_label}_skipped reason=lock_exists job_id={jid!r}")
        return

    print(
        f"{launch_label}_cmd="
        f"{' '.join(cmd)} cwd={_REPO_ROOT!r}"
    )

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=_REPO_ROOT,
            close_fds=True,
        )
    except Exception as e:
        print(f"{launch_label}_skipped reason=launch_error err={e!r}")
        _release_lock_path(lock_path)
        return

    _write_lock_child_pid(lock_path, proc.pid, jid)
    threading.Thread(
        target=_wait_child_and_release_lock,
        args=(proc, lock_path),
        daemon=True,
    ).start()
    print(f"{launch_label}_launched job_id={jid!r} pid={proc.pid}")


def maybe_launch_auto_after_answer(job_id: str | None, save_ok: bool) -> None:
    jid = str(job_id).strip() if job_id else ""
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
    _launch_resume_subprocess(
        job_id=jid,
        save_ok=save_ok,
        cmd=cmd,
        launch_label="auto_after_answer",
    )


def maybe_launch_auto_after_correction(
    job_id: str | None,
    save_ok: bool,
    *,
    incorporate_latest_answer: bool = False,
) -> None:
    jid = str(job_id).strip() if job_id else ""
    cmd = [
        sys.executable,
        _RUN_RESUME_FROM_STEP7,
        "--job-id",
        jid,
        "--push",
    ]
    if incorporate_latest_answer:
        cmd.append("--incorporate-latest-answer")
    line_user_id = os.getenv("LINE_USER_ID", "").strip()
    line_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if line_user_id and line_token:
        cmd.append("--send-line")
    _launch_resume_subprocess(
        job_id=jid,
        save_ok=save_ok,
        cmd=cmd,
        launch_label="auto_after_correction",
    )


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


def _resolve_line_context() -> dict:
    ctx = get_pending_context_from_pipeline()
    pending = state.get("pending_question")
    if isinstance(ctx, dict) and str(ctx.get("job_id") or "").strip():
        selected_unknown = ctx.get("selected_unknown")
        return {
            "source": "pipeline_context",
            "question_id": str(ctx.get("question_id") or "unknown"),
            "question_text": str(ctx.get("question_text") or "").strip(),
            "job_id": str(ctx.get("job_id") or "").strip() or None,
            "selected_unknown": selected_unknown if isinstance(selected_unknown, dict) else None,
        }
    if pending is not None:
        pending_dict = pending if isinstance(pending, dict) else {}
        pending_file = load_line_pending_context()
        question_id = str(pending_dict.get("question_id") or "unknown")
        if pending_file.get("question_id"):
            question_id = str(pending_file.get("question_id"))
        question_text = pending_dict.get("question_text")
        if isinstance(pending_file.get("question_text"), str) and pending_file["question_text"].strip():
            question_text = pending_file["question_text"].strip()
        selected_unknown = pending_file.get("selected_unknown")
        return {
            "source": "local_pending_state",
            "question_id": question_id,
            "question_text": str(question_text or "").strip(),
            "job_id": str(pending_file.get("job_id") or "").strip() or None,
            "selected_unknown": selected_unknown if isinstance(selected_unknown, dict) else None,
        }
    return {
        "source": "fallback_active_job",
        "question_id": "unknown",
        "question_text": "",
        "job_id": _resolve_active_job_id_fallback(),
        "selected_unknown": None,
    }


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
    """LINE メッセージから使える情報を抽出し、回答反映と辞書更新を必要に応じて両方行う。"""
    global state

    if "answers" not in state or not isinstance(state.get("answers"), dict):
        state["answers"] = {}

    context = _resolve_line_context()
    question_id = str(context.get("question_id") or "unknown")
    qtext_for_save = str(context.get("question_text") or "").strip()
    job_id_for_save = str(context.get("job_id") or "").strip() or None
    selected_unknown = context.get("selected_unknown")
    if not isinstance(selected_unknown, dict):
        selected_unknown = None

    _update_job_phase(
        job_id=job_id_for_save,
        phase="step_16_line_message_classify",
        status="running",
        detail={"message_preview": text[:120]},
    )
    if job_id_for_save:
        _record_job_visible_log(job_id_for_save, "Step 16: LINEメッセージ情報抽出開始")
    extraction = _extract_line_message_actions_with_claude(text, context)
    reason = str(extraction.get("reason") or "").strip()
    has_answer = bool(extraction.get("has_answer"))
    answer_text = str(extraction.get("answer_text") or "").strip()
    pairs = _normalize_correction_pairs(extraction.get("correction_pairs"))
    is_irrelevant = bool(extraction.get("is_irrelevant"))
    print("line_message_extraction=")
    print(
        {
            "has_answer": has_answer,
            "reason": reason,
            "is_irrelevant": is_irrelevant,
            "job_id": job_id_for_save,
            "question_id": question_id,
            "pair_count": len(pairs),
        }
    )
    _update_job_phase(
        job_id=job_id_for_save,
        phase="step_16_line_message_classify",
        status="success",
        detail={
            "has_answer": has_answer,
            "is_irrelevant": is_irrelevant,
            "reason": reason,
            "pair_count": len(pairs),
        },
    )
    if job_id_for_save:
        _record_job_visible_log(
            job_id_for_save,
            "Step 16: LINEメッセージ情報抽出完了 "
            f"has_answer={has_answer} pair_count={len(pairs)} "
            f"irrelevant={is_irrelevant} reason={reason or '-'}",
        )

    if has_answer and not qtext_for_save:
        has_answer = False
        answer_text = ""
    effective_answer_text = answer_text or str(text or "").strip()
    answer_save_ok = False
    answered_updates = 0
    if has_answer and qtext_for_save:
        state["answers"][question_id] = text
        print("unknown_answer_received=")
        print(
            {
                "question_id": question_id,
                "answer_text": effective_answer_text,
                "job_id": job_id_for_save,
            }
        )
        answer_save_ok = persist_answer(
            question_id=question_id,
            answer_text=effective_answer_text,
            question_text=qtext_for_save,
            user_id=user_id,
            job_id=job_id_for_save,
        )
        answered_updates = _mark_unknown_points_answered(
            job_id=job_id_for_save,
            selected_unknown=selected_unknown,
            answer_text=effective_answer_text,
            question_id=question_id,
        )
        if qtext_for_save and effective_answer_text and job_id_for_save:
            _append_to_answers_json(
                job_id_for_save,
                question_id=question_id,
                question_text=qtext_for_save,
                answer_text=effective_answer_text,
            )
            _record_job_visible_log(job_id_for_save, f"Step 16: answers.json に追記 question_id={question_id}")
        print(
            "unknown_points_answered_update="
            f"job_id={job_id_for_save!r} updated_count={answered_updates}"
        )
        if job_id_for_save:
            _record_job_visible_log(
                job_id_for_save,
                f"Step 16: 回答受信済みとして記録 answered_updates={answered_updates}",
            )

    correction_save_ok = False
    added = 0
    updated = 0
    if pairs:
        added, updated = _merge_correction_pairs(pairs)
        correction_save_ok = True
        print("correction_dict_update=")
        print(
            {
                "job_id": job_id_for_save,
                "added": added,
                "updated": updated,
                "pairs": pairs,
            }
        )
        if job_id_for_save:
            _record_job_visible_log(
                job_id_for_save,
                f"Step 16: 修正依頼反映 correction_pairs={len(pairs)} added={added} updated={updated}",
            )

    if has_answer or pairs:
        trigger = (
            "answer_and_correction"
            if has_answer and pairs
            else "answer"
            if has_answer
            else "correction"
        )
        _update_job_phase(
            job_id=job_id_for_save,
            phase="step_17_resume_trigger",
            status="running",
            detail={
                "trigger": trigger,
                "answered_updates": answered_updates,
                "correction_pairs": len(pairs),
            },
        )
        if job_id_for_save:
            if has_answer and pairs:
                _record_job_visible_log(job_id_for_save, "Step 17: 回答反映と修正辞書更新をあわせて再開します")
            elif has_answer:
                _record_job_visible_log(job_id_for_save, "Step 17: 回答反映を再開します")
            else:
                _record_job_visible_log(job_id_for_save, "Step 17: 修正辞書更新を反映して Step⑦ から再開します")
        try:
            if has_answer and pairs:
                maybe_launch_auto_after_correction(
                    job_id_for_save,
                    save_ok=(answer_save_ok and correction_save_ok),
                    incorporate_latest_answer=True,
                )
            elif has_answer:
                maybe_launch_auto_after_answer(job_id_for_save, answer_save_ok)
            else:
                maybe_launch_auto_after_correction(job_id_for_save, save_ok=correction_save_ok)
        except Exception as e:
            print(f"auto_resume_after_message_exception={e!r}")
        _update_job_phase(
            job_id=job_id_for_save,
            phase="step_17_resume_trigger",
            status="success",
            detail={"trigger": trigger},
        )
        state["pending_question"] = None
        state["remote_pending"] = None
        if has_answer and pairs:
            return "回答と修正依頼の両方を受け付けました。議事録への反映と再処理を開始します。"
        if has_answer:
            return "回答ありがとうございます。議事録への反映を再開します。"
        return "修正依頼を受け付けました。補正辞書へ反映し、再処理を開始します。"

    if job_id_for_save:
        _record_job_visible_log(job_id_for_save, "Step 16: 補正に使える情報を抽出できませんでした")
        _update_job_phase(
            job_id=job_id_for_save,
            phase="step_14_suspend",
            status="running",
            detail={"last_ignored_message_preview": text[:120]},
        )
    return "今回は議事録補正とは関係ないメッセージとして扱いました。直前の質問への回答か、修正依頼を送ってください。"


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


def _send_line_push(text: str) -> None:
    """LINE push message API でメッセージを送信する。
    LINE_USER_ID と LINE_CHANNEL_ACCESS_TOKEN が未設定の場合はログのみ残してスキップ。
    """
    channel_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    line_user_id = os.getenv("LINE_USER_ID", "").strip()
    if not channel_token or not line_user_id:
        print(
            f"_send_line_push skipped: LINE_USER_ID={'set' if line_user_id else 'unset'} "
            f"LINE_CHANNEL_ACCESS_TOKEN={'set' if channel_token else 'unset'}"
        )
        return
    payload = {
        "to": line_user_id,
        "messages": [{"type": "text", "text": text}],
    }
    try:
        resp = requests.post(
            LINE_PUSH_URL,
            headers={
                "Authorization": f"Bearer {channel_token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"_send_line_push error: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"_send_line_push exception: {e!r}")


def _handle_webhook_background(text: str, user_id: str) -> None:
    """BackgroundTasks から呼び出されるワーカー。
    handle_user_input() を実行し、結果を push message API で送信する。
    エラー時もユーザーに push で通知する。
    """
    try:
        reply_text = handle_user_input(text, user_id=user_id or None)
        _send_line_push(reply_text)
    except Exception as e:
        print(f"_handle_webhook_background error: {e!r}")
        _send_line_push("処理中にエラーが発生しました。しばらく経ってから再度お試しください。")


@app.post("/callback")
async def callback(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()
    print(body)

    try:
        events = body.get("events") or []
        if not events:
            return {"status": "ok"}

        evt = events[0]
        message = evt.get("message") or {}

        if message.get("type") != "text":
            return {"status": "ok"}

        text = message.get("text")
        user_id = str((evt.get("source") or {}).get("userId") or "")
        if text is None:
            return {"status": "ok"}

        background_tasks.add_task(_handle_webhook_background, text, user_id)
    except Exception as e:
        print(f"LINE callback parse exception: {e}")

    return {"status": "ok"}
