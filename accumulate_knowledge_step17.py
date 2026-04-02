"""Step⑰: ナレッジ蓄積モジュール。

Step⑪（議事録生成）完了直後に実行され、現在のジョブで得られた回答（answers.json）を
Knowledge Sheet に永続化する。

目的は将来の別ジョブで同じ質問を繰り返さないようにすること。
この処理は次回以降のジョブのためであり、現在のジョブの補正には影響しない。

環境変数:
  ANTHROPIC_API_KEY   — Anthropic API キー（必須）
  KNOWLEDGE_SHEET_ID  — Google Sheets のスプレッドシート ID（未設定時はスキップ）
"""

import json
import os

from ai_correct_text import _append_visible_log
from knowledge_sheet_store import merge_all_answers_into_knowledge_store

ANSWERS_JSON_FILENAME = "answers.json"


def load_job_answers(job_dir: str) -> list[dict]:
    """ジョブディレクトリの answers.json を読み込む。存在しなければ空リスト。"""
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
    """answers.json を読み込んで Knowledge Sheet に反映する（Step⑰）。

    失敗してもパイプラインを止めない（結果は dict で返す）。

    Args:
        job_dir: ジョブディレクトリのパス（answers.json の格納先）
        visible_log_path: processing_visible_log.txt のパス

    Returns:
        {"updated": bool, "enabled": bool, ...} の結果辞書
    """
    answers = load_job_answers(job_dir)
    if not answers:
        _append_visible_log(visible_log_path, "Step 17: ナレッジ蓄積: 回答なし → スキップ")
        return {"skipped": True, "reason": "no_answers", "enabled": True, "updated": False}

    _append_visible_log(
        visible_log_path,
        f"Step 17: ナレッジ蓄積開始 answers={len(answers)}件",
    )

    try:
        result = merge_all_answers_into_knowledge_store(answers)
    except Exception as e:
        _append_visible_log(visible_log_path, f"Step 17: ナレッジ蓄積: エラー → {e!r}")
        return {"error": str(e), "enabled": True, "updated": False}

    if result.get("skipped"):
        reason = str(result.get("reason") or "").strip() or "-"
        _append_visible_log(
            visible_log_path,
            f"Step 17: ナレッジ蓄積: スキップ reason={reason}",
        )
    elif not result.get("enabled"):
        _append_visible_log(
            visible_log_path,
            "Step 17: ナレッジ蓄積: スキップ（KNOWLEDGE_SHEET_ID未設定）",
        )
    elif result.get("updated"):
        before = result.get("knowledge_count_before", 0)
        after = result.get("knowledge_count_after", 0)
        _append_visible_log(
            visible_log_path,
            f"Step 17: ナレッジ蓄積完了 updated({before}件→{after}件)",
        )
    else:
        reason = str(result.get("reason") or "").strip() or "-"
        _append_visible_log(
            visible_log_path,
            f"Step 17: ナレッジ蓄積完了 unchanged reason={reason}",
        )

    return result
