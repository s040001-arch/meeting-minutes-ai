"""Step 4.35: Claude 4 Opus による AI 不明点検出モジュール。"""

import json
import logging
import os
import re

from ai_correct_text import (
    _append_visible_log,
    _normalize_api_key,
    _stream_anthropic_text,
)
from knowledge_sheet_store import load_knowledge_memos
from meeting_profile import format_meeting_profile_for_prompt
from unknown_point_filters import filter_answerable_unknown_points

logger = logging.getLogger(__name__)

OPUS_DETECTION_MODEL = "claude-opus-4-20250514"


def _resolve_detection_api_key() -> str:
    key = _normalize_api_key(os.getenv("ANTHROPIC_API_KEY"))
    if key:
        return key
    raise RuntimeError("ANTHROPIC_API_KEY is not set for AI unknown-point detection.")


def _format_knowledge_as_exclusion(memos: list[str] | None) -> str:
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


def _build_detection_prompt(
    meeting_profile: dict,
    knowledge_memos: list[str],
    answered_items: list[dict] | None = None,
    knowledge_block_override: str | None = None,
) -> str:
    profile_block = format_meeting_profile_for_prompt(meeting_profile)
    # Phase 2: Layer 2 由来の整形済テキストがあれば優先(顧客/参加者でフィルタ済み)。
    if knowledge_block_override is not None and knowledge_block_override.strip():
        knowledge_block = knowledge_block_override
    else:
        knowledge_block = _format_knowledge_as_exclusion(knowledge_memos)

    scope = str(meeting_profile.get("meeting_scope") or "unknown")

    if scope == "external":
        purpose_frame = (
            "この会議は外部会議（顧客との打ち合わせ）です。"
            "目的は、議事録（逐語録）の精度を上げるために、"
            "相原本人に確認すれば確定できる箇所だけを不明点として抽出することです。"
            "顧客との未決定の宿題・これから決める事項は、"
            "議事録に『未定』等と書けば足りるため、不明点に含めません。"
        )
    elif scope == "internal":
        purpose_frame = (
            "この会議は社内会議（プレセナ社内の議論）です。"
            "目的は、議事録の精度を上げるために、"
            "相原本人に確認すれば確定できる箇所だけを不明点として抽出することです。"
        )
    else:
        purpose_frame = (
            "目的は、議事録の精度を上げるために、"
            "相原本人に確認すれば確定できる箇所だけを不明点として抽出することです。"
        )

    prompt = (
        "あなたは議事録（逐語録）の品質管理アシスタントです。\n"
        "このステップの唯一の目的は、会議で実際に発言された内容を"
        "正確な文字起こしとして残すことです。\n"
        "会議で言われていないこと・決まっていないこと・これから決めることを"
        "ユーザーに確認したり、議事録に書き足したりするのは目的外です。\n"
        f"{purpose_frame}\n"
        f"{profile_block}\n"
        "\n【優先度評価（proposal_impact = 議事録の正確さへの影響）】\n"
        "各候補に proposal_impact スコア（1-10）を付けてください：\n"
        "- 10: 固有名詞・数値の誤認識で、議事録の意味が大きく変わる\n"
        "- 7-9: 聞き取り誤り・表記ゆれで、文脈理解に影響する\n"
        "- 4-6: 補正があれば議事録が明確になるが、なくても大筋は読める\n"
        "- 1-3: 軽微な表記ゆれ\n"
        "\n【大前提】\n"
        "抽出するのは「音声認識・文字起こし上、正しい表記が確定できない箇所」だけです。\n"
        "相原本人に聞けば正しい表記・正しい固有名詞が分かるものに限ります。\n"
        "会議で『未定』『検討中』『これから決める』と言われた内容は、"
        "議事録にそのまま書けば足ります。不明点に含めません。\n"
        "\n【検出対象（文字起こしの正確性のみ）】\n"
        "1. 固有名詞・人名・組織名・製品名で、音声認識後も正しい表記が確定できないもの\n"
        "2. 数値・金額・人数で、聞き取りが曖昧または誤変換の疑いがあるもの\n"
        "3. 意味が通らない語句・明らかな誤変換の疑いがある箇所\n"
        "\n【除外（目的外・質問しても無意味）】\n"
        "- 会議内で『未定』『これから決める』『今後検討』『次回までに』のように、"
        "答えがまだ存在しないと示されている論点（相原に聞いても答えられない）\n"
        "- 会議内で『顧客に確認する』『先方に聞く』『持ち帰る』と決まった事項"
        "（答えを持つのは相原ではなく顧客側・将来の検討であり、質問先が誤っている）\n"
        "- まだ候補・選択肢すら挙がっていない、将来決定する予定の事項\n"
        "- 実施時期・スケジュール・日程が『検討中』『AかBかで未定』"
        "『最終的な時期は未定』等と記録されている論点"
        "（議事録にそのまま書けば足り、相原に選ばせても答えられない）\n"
        "- 複数の時期案（例: 10月か年末年始）が挙がっているが、"
        "どちらで進めるか決まっていない・決めていない論点\n"
        "- 相槌・つなぎ言葉の聞き取り誤り\n"
        "- 会議の本筋に関係ない雑談\n"
        "- 既に会議内で合意・確認が取れている事項\n"
        "- 具体的な日時・時期・スケジュール（例：「6月1週目」「6月中」「来月末まで」）が"
        "会議内で発言されている場合、その時期に関する論点は不明点として扱わない。"
        "時期が合意された証拠が evidence にある場合は、その不明点を抽出してはならない。\n"
        "- 数値・金額・人数・期間などが会議内で具体的に発言・合意されている場合、"
        "それを「未確定」として抽出してはならない。\n"
        "- 金額が会話に出ていても、文脈が『交渉・相談・内示・落としどころ・来期予算・"
        "方向感（例: 70万ならいけるかな）』であれば、文字起こしの誤りではなく"
        "未決定のビジネス判断。抽出禁止。\n"
        "- 『早めに宮本講師を抑える』等、時期が曖昧なままの発言を"
        "具体日・具体時期に確定させる論点は抽出禁止。\n"
        "- 「○○については別途」「この話はまた」と先送りされた論点（＝この場で答えは決まっておらず、"
        "相原に聞いても確定した回答は得られないため抽出しない）\n"
        "- 下記の既知情報に該当するもの\n"
        f"{knowledge_block}\n"
        "\n【出力形式】\n"
        "JSON配列のみを出力してください。説明文・前置き・マークダウンのコードブロックは不要です。\n"
        "各要素は以下のフィールドを持つ：\n"
        "- type: 'name_unclear' / 'fact_unknown' のいずれか（文字起こし上の表記・意味の不明のみ）\n"
        "- text: 何の表記・語句が確定できないか（1文）\n"
        "- proposal_impact: 1-10 の整数\n"
        "- reason: なぜこの箇所が議事録の正確さに影響するか（簡潔に）\n"
        "- evidence: 該当語句の前後を含む会議発言の引用（30-100文字）。"
        "聞き取りが曖昧な語句・固有名詞・数値の前後を引用すること。"
        "『未定』『検討中』『決まっていない』等の未決定を示す発言だけを evidence に"
        "使うのは禁止（その論点はそもそも抽出しない）。"
        "冒頭挨拶・議題提示文を evidence に使うのも禁止。\n"
        "- hypothesis: 正しい表記の推測（固有名詞・数値の聞き取り誤りの表記確認に限る。"
        "交渉方針・予算・来期の方向感の Yes/No 確認に使ってはならない）\n"
        "\n最大10件。proposal_impact の降順でソート。"
        "文字起こしの正確性に関係ない候補は1件も含めない。"
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

    return prompt


def _parse_proposal_impact(value: object) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(10, n))


def _dedupe_unknown_items(items: list[dict]) -> list[dict]:
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
    meeting_profile: dict | None = None,
    answered_items: list[dict] | None = None,
    visible_log_path: str | None = None,
    # 後方互換（未使用）
    filename_hints: list[str] | None = None,
    job_context: dict | None = None,
) -> list[dict]:
    if not text:
        return []

    resolved_model = (
        model
        or os.environ.get("ANTHROPIC_DETECTION_MODEL", "").strip()
        or OPUS_DETECTION_MODEL
    )

    try:
        api_key = _resolve_detection_api_key()
    except RuntimeError:
        _append_visible_log(visible_log_path, "  AI不明点検出: APIキーが未設定のためスキップ")
        return []

    profile = dict(meeting_profile or {})
    knowledge_memos: list[str] = list(profile.get("relevant_knowledge") or [])
    # Phase 2: Layer 2 由来の知識ブロックを取得(空ならレガシー memos にフォールバック)
    knowledge_block_override = ""
    try:
        from world_knowledge_store import get_runtime_knowledge_block
        knowledge_block_override = get_runtime_knowledge_block(
            meeting_profile=profile, purpose="detection",
        )
        if knowledge_block_override.strip():
            _append_visible_log(
                visible_log_path,
                f"  関連知識を読み込みました（{len(knowledge_block_override):,}文字）",
            )
    except Exception as e:
        _append_visible_log(visible_log_path, f"  ナレッジ読み込みエラー（検出は続行）: {e!r}")

    _append_visible_log(
        visible_log_path,
        f"  AIに不明点の検出を依頼中...（{len(text):,}文字を分析）",
    )

    system_prompt = _build_detection_prompt(
        meeting_profile=profile,
        knowledge_memos=knowledge_memos,
        answered_items=answered_items,
        knowledge_block_override=knowledge_block_override,
    )

    try:
        raw_response, _stop_reason = _stream_anthropic_text(
            api_key=api_key,
            model=resolved_model,
            system_prompt=system_prompt,
            user_message=text,
            max_tokens=4096,
            timeout_sec=timeout_sec,
            log_label="detect_unknown_points",
        )
    except Exception as e:
        _append_visible_log(visible_log_path, f"  AI不明点検出: APIエラー（続行）: {e!r}")
        return []

    raw_text = raw_response.strip()
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", raw_text, re.DOTALL)
    if m:
        raw_text = m.group(1)

    try:
        result = json.loads(raw_text)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", raw_text, re.DOTALL)
        if not m:
            _append_visible_log(visible_log_path, "  AI不明点検出: 応答解析に失敗")
            return []
        try:
            result = json.loads(m.group(0))
        except json.JSONDecodeError:
            _append_visible_log(visible_log_path, "  AI不明点検出: 応答解析に失敗")
            return []

    if not isinstance(result, list):
        _append_visible_log(visible_log_path, "  AI不明点検出: 応答形式が想定外")
        return []

    aggregated: list[dict] = []
    _TRANSCRIPTION_TYPES = {"name_unclear", "fact_unknown"}
    for item in result:
        if not isinstance(item, dict):
            continue
        text_val = str(item.get("text", "")).strip()
        reason_val = str(item.get("reason", "")).strip()
        if not text_val or not reason_val:
            continue
        raw_type = str(item.get("type", "fact_unknown")).strip() or "fact_unknown"
        # 文字起こし精度以外の旧型（意思決定・前提確認等）は受理しない
        if raw_type not in _TRANSCRIPTION_TYPES:
            continue
        entry: dict = {
            "type": raw_type,
            "text": text_val,
            "reason": reason_val,
            "proposal_impact": _parse_proposal_impact(item.get("proposal_impact")),
            "source": "claude_step9",
        }
        evidence = str(item.get("evidence", "")).strip()
        if evidence:
            entry["evidence"] = evidence[:200]
        hypothesis = str(item.get("hypothesis", "")).strip()
        if hypothesis:
            entry["hypothesis"] = hypothesis
        aggregated.append(entry)

    deduped = _dedupe_unknown_items(aggregated)
    deduped, dropped_non_answerable = filter_answerable_unknown_points(deduped)
    deduped.sort(key=lambda x: int(x.get("proposal_impact") or 0), reverse=True)
    deduped = deduped[:10]
    _append_visible_log(
        visible_log_path,
        f"  AI不明点検出が完了しました（{len(deduped)}件を検出"
        f"{f'、未決定論点{dropped_non_answerable}件を除外' if dropped_non_answerable else ''}）",
    )
    return deduped
