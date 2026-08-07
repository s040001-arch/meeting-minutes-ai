"""事前情報（メール・アジェンダ等）の取り込み（2026-08-05 ユーザー要望）。

会議前にメール等で論点・参加者・固有名詞が共有されていることがあり、
これを最初に取り込むと固有名詞の誤変換修正・意味不明判定・質問数削減の
すべてに効く。フローは:

1. ジョブ開始時に LINE で「事前の情報共有があれば送ってください」と依頼し、
   pending context (kind=prior_context_request) を書く。
2. ユーザーが LINE でメール本文等を送ると、webhook がこのモジュールの
   ingest_prior_context() を呼ぶ。
3. LLM で再利用可能な知識メモに要約し、
   - ナレッジシート（実行時に読むステージ用）
   - ジョブの meeting_profile.json の relevant_knowledge（プロファイル参照ステージ用）
   の両方へ反映する。原文は job_dir/prior_context.txt に保存する。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any

_EXTRACT_MODEL = "claude-sonnet-5"

PENDING_KIND = "prior_context_request"
PRIOR_CONTEXT_FILENAME = "prior_context.txt"
LINE_PENDING_CONTEXT_PATH = os.path.join("data", "line_pending_context.json")

_NO_CONTEXT_ANSWERS = {
    "なし", "ない", "無し", "特になし", "特にない", "特にないです", "ないです",
    "特にありません", "ありません", "no", "ok", "大丈夫", "大丈夫です",
}


def is_no_context_reply(text: str) -> bool:
    s = re.sub(r"[。．\.！!\s]", "", str(text or "")).strip().lower()
    return s in _NO_CONTEXT_ANSWERS


def build_request_message(display_title: str | None) -> str:
    title = str(display_title or "").strip()
    head = f"「{title}」の処理を開始しました。" if title else "議事録の処理を開始しました。"
    return (
        f"📩 {head}\n"
        "事前の情報共有（メール・アジェンダ・参加者情報など）があれば、"
        "このままLINEに貼り付けて送ってください。"
        "固有名詞や論点の認識精度が上がります。\n"
        "なければ「なし」と返信してください。"
    )


def write_prior_context_pending(job_id: str) -> None:
    """webhook が事前情報の返信を識別できるよう pending context を書く。"""
    payload = {
        "written_at": datetime.now(timezone.utc).isoformat(),
        "kind": PENDING_KIND,
        "job_id": str(job_id),
        "question_id": PENDING_KIND,
        "question_text": "",
        "selected_unknown": None,
    }
    os.makedirs(os.path.dirname(LINE_PENDING_CONTEXT_PATH) or ".", exist_ok=True)
    with open(LINE_PENDING_CONTEXT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    _sync_pending_to_remote(payload)


def _sync_pending_to_remote(payload: dict) -> None:
    """webhook が別プロセス/別ホストの場合に remote_pending も更新する。

    run_question_cycle_once._sync_line_pending_to_remote と同じ仕組み。
    未設定なら何もしない（同一プロセスならファイルで足りる）。
    """
    url = os.getenv("LINE_PENDING_SYNC_URL", "").strip()
    if not url:
        return
    import requests

    headers = {"Content-Type": "application/json"}
    secret = os.getenv("LINE_PENDING_SYNC_SECRET", "").strip()
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=20)
        if r.status_code != 200:
            print(
                f"prior_context_pending_sync_http_error status={r.status_code}"
            )
    except Exception as e:  # noqa: BLE001
        print(f"prior_context_pending_sync_failed={e!r}")


def clear_prior_context_pending() -> None:
    """pending が prior_context_request のままなら消す（質問 pending は消さない）。"""
    try:
        if not os.path.isfile(LINE_PENDING_CONTEXT_PATH):
            return
        with open(LINE_PENDING_CONTEXT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("kind") == PENDING_KIND:
            os.remove(LINE_PENDING_CONTEXT_PATH)
    except Exception as e:  # noqa: BLE001
        print(f"clear_prior_context_pending_failed={e!r}")


def request_prior_context(job_id: str, display_title: str | None) -> bool:
    """ジョブ開始時に LINE で事前情報を依頼する。成功なら True。"""
    try:
        from question_mode import resolve_question_mode

        if resolve_question_mode() == "cursor":
            print(
                "prior_context_request skipped: "
                "QUESTION_MODE=cursor (no LINE side effects)"
            )
            return False
    except Exception:
        pass
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    user_id = os.getenv("LINE_USER_ID", "").strip()
    if not token or not user_id:
        print("prior_context_request skipped: LINE env not set")
        return False
    from line_send_question import push_line_message

    write_prior_context_pending(job_id)
    push_line_message(token, user_id, build_request_message(display_title))
    return True


def _extract_memos_with_llm(text: str, profile: dict[str, Any]) -> list[str]:
    import anthropic

    customer = str(profile.get("customer_name") or "").strip()
    topic = str(profile.get("topic") or "").strip()
    hint = ""
    if customer or topic:
        hint = f"\n今回の会議: 顧客={customer or '不明'} / 議題={topic or '不明'}"
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=_EXTRACT_MODEL,
        max_tokens=4000,
        timeout=120,
        system=(
            "あなたは議事録AIのナレッジ管理アシスタントです。"
            "会議前に共有されたメールや資料の本文から、議事録の音声認識補正と"
            "文脈理解に役立つ知識を抽出してください。対象:"
            "(a) 参加者の氏名・所属・役割、(b) 組織名・サービス名・事業名、"
            "(c) 会議の議題・論点・背景、(d) 固有の用語・略語・数値。"
            "音声認識で誤変換されやすい固有名詞は「〜と誤認識されることがある」"
            "という形で候補読みも添えてください。"
            "1件1行の自由記述メモとして、他の会議でも意味が通じる自己完結した"
            "文で書いてください。URL や講座の細かいリストは要約してよい。"
            "件数は重要なものに絞って最大15件。"
            '出力はJSON配列のみ: ["メモ1", "メモ2", ...]' + hint
        ),
        messages=[{"role": "user", "content": str(text)[:20000]}],
    )
    body = "".join(
        b.text for b in resp.content if getattr(b, "type", "") == "text"
    )
    m = re.search(r"\[.*\]", body, re.DOTALL)
    if not m:
        return []
    return [
        " ".join(str(x).split())
        for x in json.loads(m.group(0))
        if str(x).strip()
    ]


def _append_to_job_profile(job_dir: str, memos: list[str]) -> int:
    """ジョブの meeting_profile.json の relevant_knowledge に追記する。"""
    path = os.path.join(job_dir, "meeting_profile.json")
    if not os.path.isfile(path):
        return 0
    with open(path, "r", encoding="utf-8") as f:
        profile = json.load(f)
    existing = list(profile.get("relevant_knowledge") or [])
    added = [m for m in memos if m not in existing]
    if not added:
        return 0
    profile["relevant_knowledge"] = existing + added
    with open(path, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)
    return len(added)


def ingest_prior_context(text: str, job_id: str) -> dict[str, Any]:
    """LINE で届いた事前情報を保存・要約し、ナレッジへ反映する。"""
    job_dir = os.path.join("data", "transcriptions", str(job_id))
    result: dict[str, Any] = {
        "job_id": job_id,
        "raw_saved": False,
        "memos_extracted": 0,
        "sheet_added": 0,
        "profile_added": 0,
    }

    # 1. 原文保存（LLM が失敗しても原文は残す）
    try:
        os.makedirs(job_dir, exist_ok=True)
        raw_path = os.path.join(job_dir, PRIOR_CONTEXT_FILENAME)
        with open(raw_path, "a", encoding="utf-8") as f:
            f.write(str(text).rstrip() + "\n\n")
        result["raw_saved"] = True
    except OSError as e:
        print(f"prior_context_raw_save_failed={e!r}")

    # 2. LLM でメモ抽出
    profile: dict[str, Any] = {}
    profile_path = os.path.join(job_dir, "meeting_profile.json")
    if os.path.isfile(profile_path):
        try:
            with open(profile_path, "r", encoding="utf-8") as f:
                profile = json.load(f)
        except Exception:  # noqa: BLE001
            profile = {}
    memos = _extract_memos_with_llm(text, profile)
    result["memos_extracted"] = len(memos)
    if not memos:
        return result

    # 3. ナレッジシートへ追記（実行時にシートを読むステージ用）
    try:
        from knowledge_sheet_store import load_knowledge_memos, save_knowledge_memos

        existing = load_knowledge_memos()
        added = [m for m in memos if m not in existing]
        if added:
            save_knowledge_memos(existing + added)
        result["sheet_added"] = len(added)
    except Exception as e:  # noqa: BLE001
        print(f"prior_context_sheet_update_failed={e!r}")

    # 4. ジョブの meeting_profile.json へ追記（プロファイル参照ステージ用）
    try:
        result["profile_added"] = _append_to_job_profile(job_dir, memos)
    except Exception as e:  # noqa: BLE001
        print(f"prior_context_profile_update_failed={e!r}")

    return result
