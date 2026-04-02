"""ジョブごとのコンテキスト情報管理モジュール。

ジョブディレクトリに context.json を配置すると、AI補正プロンプトに
参加者・議題・関連企業などの情報が注入される。

context.json フォーマット（全フィールドオプション）:
{
  "participants":       ["相原 隆太郎", "高橋季央", "加藤万紀子"],
  "related_companies":  ["THR", "デイシス", "矢崎グループ"],
  "agenda":             ["仕事力サーベイのTHR展開", "グループ会社への展開戦略"],
  "notes":              "その他の補足情報"
}
"""

import json
import os
from typing import Any

CONTEXT_FILENAME = "context.json"


def load_job_context(job_dir: str) -> dict[str, Any]:
    """ジョブディレクトリから context.json を読み込む。存在しなければ空 dict。"""
    path = os.path.join(job_dir, CONTEXT_FILENAME)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except (OSError, json.JSONDecodeError):
        return {}


def _build_context_lines(context: dict[str, Any]) -> list[str]:
    parts: list[str] = []
    participants = [str(p).strip() for p in (context.get("participants") or []) if str(p).strip()]
    if participants:
        parts.append(f"参加者: {', '.join(participants)}")
    related = [str(c).strip() for c in (context.get("related_companies") or []) if str(c).strip()]
    if related:
        parts.append(f"関連企業・組織: {', '.join(related)}")
    agenda = [str(a).strip() for a in (context.get("agenda") or []) if str(a).strip()]
    if agenda:
        parts.append(f"議題: {'; '.join(agenda)}")
    notes = str(context.get("notes") or "").strip()
    if notes:
        parts.append(f"補足: {notes}")
    return parts


def format_context_for_detection_prompt(context: dict[str, Any]) -> str:
    """検出プロンプト専用のコンテキスト注入。
    参加者名・関連企業名の誤変換を積極的に検出させる。
    """
    if not context:
        return ""
    parts = _build_context_lines(context)
    if not parts:
        return ""
    content = "\n".join(parts)
    return (
        "\n\n【この会議のコンテキスト（検出に活用してください）】\n"
        f"{content}\n"
        "上記の参加者名・企業名が音声認識で誤変換されている場合は積極的に検出し、"
        "guess_level を 0〜10 に設定してください。"
    )


def format_context_for_prompt(context: dict[str, Any]) -> str:
    """補正プロンプト（全文整形）用のコンテキスト注入。
    参加者名・企業名の文脈理解と誤変換修正に使用する。
    """
    if not context:
        return ""
    parts = _build_context_lines(context)
    if not parts:
        return ""
    content = "\n".join(parts)
    return (
        "\n\n【この会議のコンテキスト】\n"
        f"{content}\n"
        "上記の参加者名・企業名が音声認識で誤変換されている場合は正しい表記に修正してください。"
    )
