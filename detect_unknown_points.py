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
from knowledge_sheet_store import load_knowledge_memos

logger = logging.getLogger(__name__)

OPUS_DETECTION_MODEL = "claude-opus-4-20250514"
# 旧 30,000 字制限を撤廃。Claude Opus のコンテキスト窓 200K に収まる範囲で扱う。
# 安全側で 100,000 字までを単一プロンプトで処理し、それ以上は分割検出する。
_DETECTION_MAX_TEXT_CHARS = 100000
_DETECTION_CHUNK_CHARS = 80000  # 分割時の1チャンク文字数（オーバーラップ含めず）
_DETECTION_CHUNK_OVERLAP = 1500  # チャンク間の重複文字数（境界の不明点を取り逃さない）


def _resolve_detection_api_key() -> str:
    key = _normalize_api_key(os.getenv("ANTHROPIC_API_KEY"))
    if key:
        return key
    raise RuntimeError("ANTHROPIC_API_KEY is not set for AI unknown-point detection.")


def _format_knowledge_as_exclusion(memos: list[str] | None) -> str:
    """ナレッジを「既知情報＝質問しないリスト」として明示する。

    `format_knowledge_for_prompt` は補正用の参考情報文言なので、
    検出時は「これらは既知ゆえ確認質問するな」を強く伝える別フォーマットを使う。
    """
    if not memos:
        return ""
    lines = "\n".join(f"- {m}" for m in memos if m and m.strip())
    if not lines:
        return ""
    return (
        "\n\n【既知情報（これらに関する質問を生成してはならない）】\n"
        "以下はスプレッドシートに既に登録されている用語・人名・組織・社内呼称等です。\n"
        "これらに該当する内容は、たとえ会議テキスト上で表記揺れがあっても、\n"
        "『不明点』として挙げないでください（後段の補正処理で自動的に正しい表記へ修正されます）。\n"
        f"{lines}"
    )


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
        "5. 金額・件数・期限などの数値で誤認識・誤聴き取りの疑いがあるもの\n"
        "\n【除外するもの（重要・厳守）】\n"
        "- 言い淀み・フィラー（「えっと」「あの」等）\n"
        "- 文脈から一意に解釈できる箇所\n"
        "- 同一論点の重複（1論点につき1件のみ）\n"
        "- 後述の【既知情報】に登録済みの用語・人名・組織（後述ナレッジで自動補正される）\n"
        "- 後述の【確認済み情報】で過去に同等の質問が解消済みのもの\n"
        "\n【優先度の付け方】\n"
        "- 議事録の主要論点・合意事項・Next Action に関連するものを優先\n"
        "- 単なる雑談中の固有名詞は優先度を下げる\n"
        "- 確認したら本文の何箇所も連動して確定する『要』の論点を最優先\n"
        "\n【hypothesis（推測の正解）について】\n"
        "- 文脈から「これではないか」と高い確度で推測できる場合は必ず hypothesis に書く\n"
        "- hypothesis があれば後段で『○○で合っていますか？』のYes/No形式に変換される\n"
        "- 自由記述質問は回答コストが高いため、可能な限り hypothesis を埋めること\n"
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
        for item in answered_items[:30]:
            q_text = str(item.get("text", "")).strip()[:120]
            answer = str(item.get("answer", "")).strip()[:120]
            if q_text and answer:
                confirmed_lines.append(f"- 「{q_text}」→ 回答: {answer}")
        if confirmed_lines:
            prompt += (
                "\n\n【確認済み情報（重複質問禁止・厳守）】\n"
                "以下は過去のQ&Aで既に確認済みの内容です。同じ内容を再度不明点として挙げないでください。\n"
                + "\n".join(confirmed_lines)
            )

    prompt += format_hints_for_prompt(filename_hints or [])
    prompt += format_context_for_prompt(job_context or {})
    prompt += _format_knowledge_as_exclusion(knowledge_memos or [])
    return prompt


def _split_text_for_detection(text: str) -> list[str]:
    """長文を検出用に分割する。文字単位で重複付きチャンクに分ける。"""
    if len(text) <= _DETECTION_MAX_TEXT_CHARS:
        return [text]
    chunks: list[str] = []
    pos = 0
    while pos < len(text):
        end = min(pos + _DETECTION_CHUNK_CHARS, len(text))
        chunk = text[pos:end]
        chunks.append(chunk)
        if end >= len(text):
            break
        pos = end - _DETECTION_CHUNK_OVERLAP
        if pos < 0:
            pos = 0
    return chunks


def _dedupe_unknown_items(items: list[dict]) -> list[dict]:
    """text 完全一致で重複削除。先勝ち。"""
    seen: set[str] = set()
    out: list[dict] = []
    for item in items:
        key = str(item.get("text", "")).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


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

    chunks = _split_text_for_detection(text)
    if len(chunks) > 1:
        _append_visible_log(
            visible_log_path,
            f"  AIに不明点の検出を依頼中...（{len(text):,}文字を{len(chunks)}分割で分析）",
        )
    else:
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

    max_tokens = 4096
    aggregated: list[dict] = []

    for chunk_idx, chunk_text in enumerate(chunks):
        if len(chunks) > 1:
            _append_visible_log(
                visible_log_path,
                f"  チャンク{chunk_idx + 1}/{len(chunks)}を分析中...（{len(chunk_text):,}文字）",
            )
        try:
            raw_response, _stop_reason = _stream_anthropic_text(
                api_key=api_key,
                model=resolved_model,
                system_prompt=system_prompt,
                user_message=chunk_text,
                max_tokens=max_tokens,
                timeout_sec=timeout_sec,
                log_label=f"detect_unknown_points_chunk_{chunk_idx + 1}",
            )
        except Exception as e:
            _append_visible_log(
                visible_log_path,
                f"  AI不明点検出: APIエラー（チャンク{chunk_idx + 1}スキップ、続行）: {e!r}",
            )
            continue

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
                        f"  AI不明点検出: チャンク{chunk_idx + 1}の応答解析に失敗（スキップ）",
                    )
                    continue
            else:
                _append_visible_log(
                    visible_log_path,
                    f"  AI不明点検出: チャンク{chunk_idx + 1}に有効データなし（スキップ）",
                )
                continue

        if not isinstance(result, list):
            _append_visible_log(
                visible_log_path,
                f"  AI不明点検出: チャンク{chunk_idx + 1}の応答形式が想定外（スキップ）",
            )
            continue

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
            aggregated.append(entry)

    deduped = _dedupe_unknown_items(aggregated)
    # 全体上限を 12 件に拡張（hypothesis 付き Yes/No 質問が増える前提で余裕を持たせる）
    deduped = deduped[:12]
    _append_visible_log(
        visible_log_path,
        f"  AI不明点検出が完了しました（{len(deduped)}件を検出）",
    )
    return deduped
