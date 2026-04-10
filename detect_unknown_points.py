"""Step⑨: Claude 4 Opus による AI 不明点検出モジュール。

Step⑧の補正済みテキストを入力として、文脈レベルの不明点を検出する。

出力スキーマは extract_unknown_points.py の形式（type/text/reason）に準拠し、
source/hypothesis を追加フィールドとして持つ。
パース失敗時は空リストを返してパイプラインを止めない。

環境変数:
  ANTHROPIC_API_KEY          — Anthropic API キー（必須）
  ANTHROPIC_DETECTION_MODEL  — モデル名の上書き（デフォルト: claude-opus-4-20250514）
"""

import json
import logging
import os
import re

from ai_correct_text import (
    _append_visible_log,
    _normalize_api_key,
    _stream_anthropic_text,
)
from filename_hints import format_hints_for_prompt
from job_context import format_context_for_prompt
from knowledge_sheet_store import format_knowledge_for_prompt, load_knowledge_memos

logger = logging.getLogger(__name__)

OPUS_DETECTION_MODEL = "claude-opus-4-20250514"
_DETECTION_MAX_TEXT_CHARS = 30000


def _resolve_detection_api_key() -> str:
    key = _normalize_api_key(os.getenv("ANTHROPIC_API_KEY"))
    if key:
        return key
    raise RuntimeError("ANTHROPIC_API_KEY is not set for AI unknown-point detection.")


def _build_detection_system_prompt(
    filename_hints: list[str] | None = None,
    job_context: dict | None = None,
    knowledge_memos: list[str] | None = None,
    answered_items: list[dict] | None = None,
) -> str:
    prompt = (
        "あなたは会議の議事録品質管理アシスタントです。\n"
        "以下の会議テキスト（音声認識→AI補正済み）を読み、人間の担当者が確認しなければ"
        "議事録を正確に完成させられない「不明点」を最大10件まで検出してください。\n"
        "\n【検出対象とする不明点】\n"
        "1. 人名の表記揺れ・誤認（音声認識由来の誤変換が残っている疑い）\n"
        "2. 社名・部署名・略称の正式名称が不明\n"
        "3. 決定事項・アクションアイテムの実施主体（誰がやるか）が不明確\n"
        "4. 発言の文脈から意味が複数解釈できる箇所\n"
        "\n【除外するもの】\n"
        "- 言い淀み・フィラー（「えっと」「あの」等）\n"
        "- 文脈から一意に解釈できる箇所\n"
        "- 同一論点の重複（1論点につき1件のみ）\n"
        "\n【出力形式】\n"
        "JSON配列のみを出力してください。説明文・前置き・マークダウンのコードブロックは不要です。\n"
        "各要素のスキーマ:\n"
        '{"type": "固有名詞|決定事項|発言意図|数値|主語", '
        '"text": "問題箇所のテキスト（前後の文脈を含む短い抜粋。70文字以内）", '
        '"reason": "なぜ不明なのか（1〜2文）", '
        '"hypothesis": "推測される正解（確信があれば。なければ空文字）"}\n'
        "最大10件。本当に確認が必要なものだけに絞ること。"
    )

    if answered_items:
        confirmed_lines = []
        for item in answered_items[:20]:
            q_text = str(item.get("text", "")).strip()[:120]
            answer = str(item.get("answer", "")).strip()[:120]
            if q_text and answer:
                confirmed_lines.append(f"- 「{q_text}」→ 回答: {answer}")
        if confirmed_lines:
            prompt += (
                "\n\n【確認済み情報（重複質問禁止）】\n"
                "以下は過去のQ&Aで既に確認済みの内容です。同じ内容を再度不明点として挙げないでください。\n"
                + "\n".join(confirmed_lines)
            )

    prompt += format_hints_for_prompt(filename_hints or [])
    prompt += format_context_for_prompt(job_context or {})
    prompt += format_knowledge_for_prompt(knowledge_memos or [])
    return prompt


def detect_unknown_points(
    text: str,
    *,
    model: str | None = None,
    timeout_sec: int = 600,
    filename_hints: list[str] | None = None,
    job_context: dict | None = None,
    answered_items: list[dict] | None = None,
    visible_log_path: str | None = None,
) -> list[dict]:
    """Step⑨: 補正済みテキストから Claude 4 Opus で不明点を検出して返す。

    エラー時は空リストを返してパイプラインを止めない。
    出力スキーマ: [{"type", "text", "reason", "source", "hypothesis?"}]

    Args:
        text: Step⑧で補正済みのテキスト
        model: モデル名（未指定時は ANTHROPIC_DETECTION_MODEL 環境変数 or デフォルト）
        timeout_sec: API タイムアウト秒数
        filename_hints: ファイル名から抽出したヒント
        job_context: context.json のコンテキスト情報
        answered_items: 過去に回答済みの不明点（重複質問防止に使用）
        visible_log_path: processing_visible_log.txt のパス
    """
    if not text:
        return []

    resolved_model = (
        model
        or os.environ.get("ANTHROPIC_DETECTION_MODEL", "").strip()
        or OPUS_DETECTION_MODEL
    )

    try:
        api_key = _resolve_detection_api_key()
    except RuntimeError as e:
        _append_visible_log(visible_log_path, f"  AI不明点検出: APIキーが未設定のためスキップ")
        return []

    knowledge_memos: list[str] = []
    try:
        knowledge_memos = load_knowledge_memos() or []
        if knowledge_memos:
            _append_visible_log(
                visible_log_path,
                f"  ナレッジシートから{len(knowledge_memos)}件の知識を参照",
            )
    except Exception as e:
        _append_visible_log(visible_log_path, f"  ナレッジ読み込みエラー（検出は続行）: {e!r}")

    _append_visible_log(
        visible_log_path,
        f"  AIに不明点の検出を依頼中...（{len(text):,}文字を分析）",
    )

    system_prompt = _build_detection_system_prompt(
        filename_hints=filename_hints,
        job_context=job_context,
        knowledge_memos=knowledge_memos,
        answered_items=answered_items,
    )

    user_text = text[:_DETECTION_MAX_TEXT_CHARS]
    max_tokens = 4096

    try:
        raw_response, stop_reason = _stream_anthropic_text(
            api_key=api_key,
            model=resolved_model,
            system_prompt=system_prompt,
            user_message=user_text,
            max_tokens=max_tokens,
            timeout_sec=timeout_sec,
            log_label="detect_unknown_points",
        )
    except Exception as e:
        _append_visible_log(
            visible_log_path,
            f"  AI不明点検出: APIエラーが発生しました（0件として続行）",
        )
        return []

    raw_text = raw_response.strip()

    # マークダウンのコードブロックを除去
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", raw_text, re.DOTALL)
    if m:
        raw_text = m.group(1)

    try:
        result = json.loads(raw_text)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", raw_text, re.DOTALL)
        if m:
            try:
                result = json.loads(m.group(0))
            except json.JSONDecodeError:
                _append_visible_log(
                    visible_log_path,
                    "  AI不明点検出: 応答の解析に失敗しました（0件として続行）",
                )
                return []
        else:
            _append_visible_log(
                visible_log_path,
                "  AI不明点検出: 応答にデータが含まれていませんでした（0件として続行）",
            )
            return []

    if not isinstance(result, list):
        _append_visible_log(
            visible_log_path,
            "  AI不明点検出: 想定外の応答形式（0件として続行）",
        )
        return []

    normalized: list[dict] = []
    for item in result:
        if not isinstance(item, dict):
            continue
        text_val = str(item.get("text", "")).strip()
        type_val = str(item.get("type", "固有名詞")).strip()
        reason_val = str(item.get("reason", "")).strip()
        if not text_val or not reason_val:
            continue
        entry: dict = {
            "type": type_val,
            "text": text_val,
            "reason": reason_val,
            "source": "claude_step9",
        }
        hypothesis = str(item.get("hypothesis", "")).strip()
        if hypothesis:
            entry["hypothesis"] = hypothesis
        normalized.append(entry)

    normalized = normalized[:10]
    _append_visible_log(
        visible_log_path,
        f"  AI不明点検出が完了しました（{len(normalized)}件を検出）",
    )
    return normalized
