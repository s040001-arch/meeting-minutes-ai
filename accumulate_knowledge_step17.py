"""Step⑰: ナレッジ蓄積モジュール。"""

import json
import os

from ai_correct_text import _append_visible_log
from knowledge_sheet_store import merge_all_answers_into_knowledge_store
from meeting_profile import load_meeting_profile

ANSWERS_JSON_FILENAME = "answers.json"


def load_job_answers(job_dir: str) -> list[dict]:
    path = os.path.join(job_dir, ANSWERS_JSON_FILENAME)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def accumulate_knowledge(
    job_dir: str,
    *,
    visible_log_path: str | None = None,
) -> dict:
    answers = load_job_answers(job_dir)
    if not answers:
        _append_visible_log(visible_log_path, "  回答データなし → ナレッジ蓄積をスキップ")
        return {"skipped": True, "reason": "no_answers", "enabled": True, "updated": False}

    meeting_profile = load_meeting_profile(job_dir)
    _append_visible_log(
        visible_log_path,
        f"  {len(answers)}件の回答をナレッジシートに反映中..."
        f"（顧客={meeting_profile.get('customer_name') or '-'},"
        f" 議題={meeting_profile.get('topic') or '-'}）",
    )

    try:
        result = merge_all_answers_into_knowledge_store(
            answers,
            meeting_profile=meeting_profile,
        )
    except Exception as e:
        _append_visible_log(visible_log_path, f"  ナレッジ蓄積でエラーが発生しました: {e!r}")
        return {"error": str(e), "enabled": True, "updated": False}

    if result.get("skipped"):
        reason = str(result.get("reason") or "").strip() or "-"
        _append_visible_log(
            visible_log_path,
            f"  ナレッジ蓄積: スキップ（{reason}）",
        )
    elif not result.get("enabled"):
        _append_visible_log(
            visible_log_path,
            "  ナレッジ蓄積: スキップ（KNOWLEDGE_SHEET_IDが未設定のため）",
        )
    elif result.get("updated"):
        before = result.get("knowledge_count_before", 0)
        after = result.get("knowledge_count_after", 0)
        _append_visible_log(
            visible_log_path,
            f"  ナレッジ蓄積が完了しました（{before}件 → {after}件、{after - before}件追加）",
        )
    else:
        reason = str(result.get("reason") or "").strip() or "-"
        _append_visible_log(
            visible_log_path,
            f"  ナレッジ蓄積: 変更なし（{reason}）",
        )

    return result
