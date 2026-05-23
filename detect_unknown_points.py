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
) -> str:
    profile_block = format_meeting_profile_for_prompt(meeting_profile)
    knowledge_block = _format_knowledge_as_exclusion(knowledge_memos)

    scope = str(meeting_profile.get("meeting_scope") or "unknown")

    if scope == "external":
        purpose_frame = (
            "この会議は外部会議（顧客との打ち合わせ）です。"
            "相原は提案・相談を担う立場にあり、会議後に「次の打ち手」を考える必要があります。"
            "次の打ち手とは、例えば次の提案書を書く、追加のヒアリングを設計する、"
            "社内に持ち帰って検討する、などです。"
            "相原が次の打ち手を考えるために、会議の文面だけからは確定できず"
            "確認が必要な事項を不明点として抽出してください。"
        )
    elif scope == "internal":
        purpose_frame = (
            "この会議は社内会議（プレセナ社内の議論）です。"
            "相原がこの会議の議論を踏まえて次のアクションを取るために、"
            "会議の文面だけからは確定できず確認が必要な事項を不明点として抽出してください。"
        )
    else:
        purpose_frame = (
            "相原がこの会議の議論を踏まえて次のアクションを取るために、"
            "会議の文面だけからは確定できず確認が必要な事項を不明点として抽出してください。"
        )

    prompt = (
        "あなたは相原隆太郎（プレセナ・ストラテジック・パートナーズの提案担当）の参謀です。\n"
        f"{purpose_frame}\n"
        f"{profile_block}\n"
        "\n【不明点の優先度評価】\n"
        "各不明点に proposal_impact スコア（1-10）を付けてください：\n"
        "- 10: この情報がないと次の打ち手の骨子が組み立てられない（最優先）\n"
        "- 7-9: 次の打ち手のα案には書けるが、本提案・本アクションで必ず必要\n"
        "- 4-6: 打ち手の精度向上に役立つが、なくても進められる\n"
        "- 1-3: あれば嬉しいが、打ち手に大きな影響はない\n"
        "\n【検出対象】\n"
        "1. 顧客（または社内関係者）の意思決定が未確定で、打ち手の方向性に影響する論点\n"
        "2. 相手側で次回までに整理してもらう必要がある情報\n"
        "3. 相原が仮置きで話を進めた前提のうち、確認が必要なもの\n"
        "4. 重要な固有名詞・人名・組織名・数値で、音声認識補正後も意味が確定できないもの\n"
        "5. 議論の中で『〇〇については別途』『この話はまた』のように先送りされた論点\n"
        "\n【除外】\n"
        "- 相槌・つなぎ言葉の聞き取り誤り\n"
        "- 会議の本筋に関係ない雑談\n"
        "- 既に会議内で合意・確認が取れている事項\n"
        "- 具体的な日時・時期・スケジュール（例：「6月1週目」「6月中」「来月末まで」）が"
        "会議内で発言されている場合、その時期に関する論点は decision_pending として扱わない。"
        "時期が合意された証拠が evidence にある場合は、その不明点を抽出してはならない。\n"
        "- 数値・金額・人数・期間などが会議内で具体的に発言・合意されている場合、"
        "それを「未確定」として抽出してはならない。\n"
        "- 「○○については別途」「この話はまた」と先送りされた論点は deferred_topic として"
        "抽出してよいが、proposal_impact は最大 6 までとする"
        "（先送り＝相原の次の打ち手の骨子には直接影響しないため）。\n"
        "- 下記の既知情報に該当するもの\n"
        f"{knowledge_block}\n"
        "\n【出力形式】\n"
        "JSON配列のみを出力してください。説明文・前置き・マークダウンのコードブロックは不要です。\n"
        "各要素は以下のフィールドを持つ：\n"
        "- type: 'fact_unknown' / 'decision_pending' / 'name_unclear' / 'premise_to_verify' / 'deferred_topic' のいずれか\n"
        "- text: 不明点の内容（相原が読んで何を確認すべきかすぐ分かる文）\n"
        "- proposal_impact: 1-10 の整数\n"
        "- reason: なぜこの不明点が次の打ち手にとって重要なのか（簡潔に）\n"
        "- evidence: 不明点の根拠となる会議発言の該当箇所（30-100文字程度の引用）。"
        "「不明点が未確定であることを示す発言」を引用すること。"
        "冒頭挨拶・議題提示文（例：「本日は〜についてご相談」）を evidence に使うのは禁止"
        "（議題提示は不明点の根拠にならない）。\n"
        "- hypothesis: 最もありそうな答え（推測でよい）。Yes/No質問にできる場合のみ記載\n"
        "\n最大10件。proposal_impact の降順でソート。本当に確認が必要なものだけに絞る。"
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
    if not knowledge_memos:
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

    system_prompt = _build_detection_prompt(
        meeting_profile=profile,
        knowledge_memos=knowledge_memos,
        answered_items=answered_items,
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
    for item in result:
        if not isinstance(item, dict):
            continue
        text_val = str(item.get("text", "")).strip()
        reason_val = str(item.get("reason", "")).strip()
        if not text_val or not reason_val:
            continue
        entry: dict = {
            "type": str(item.get("type", "fact_unknown")).strip() or "fact_unknown",
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
    deduped.sort(key=lambda x: int(x.get("proposal_impact") or 0), reverse=True)
    deduped = deduped[:10]
    _append_visible_log(
        visible_log_path,
        f"  AI不明点検出が完了しました（{len(deduped)}件を検出）",
    )
    return deduped
