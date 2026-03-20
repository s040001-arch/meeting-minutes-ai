import json
import re
from typing import Any, Dict, List, Tuple

from openai import OpenAI

from config.settings import settings
from dictionary.abbreviation_dictionary import load_abbreviation_dictionary
from dictionary.company_dictionary import load_company_dictionary
from utils.logger import get_logger

logger = get_logger(__name__)

_MINUTES_PROMPT_MAX_TRANSCRIPT_LINES = 400


def _normalize_dictionary_items(raw: Any) -> List[Tuple[str, str]]:
    normalized: List[Tuple[str, str]] = []

    if not raw:
        return normalized

    if isinstance(raw, dict):
        for k, v in raw.items():
            if k and v:
                normalized.append((str(k).strip(), str(v).strip()))
        return normalized

    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                source = (
                    item.get("raw")
                    or item.get("before")
                    or item.get("source")
                    or item.get("input")
                    or item.get("term")
                    or item.get("abbr")
                    or item.get("abbreviation")
                    or item.get("name")
                )
                preferred = (
                    item.get("preferred")
                    or item.get("after")
                    or item.get("target")
                    or item.get("output")
                    or item.get("formal")
                    or item.get("official")
                    or item.get("expanded")
                    or item.get("normalized")
                )
                if source and preferred:
                    normalized.append((str(source).strip(), str(preferred).strip()))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                source = str(item[0]).strip()
                preferred = str(item[1]).strip()
                if source and preferred:
                    normalized.append((source, preferred))

    return normalized


def _build_dictionary_rule_text(
    company_dictionary: List[Tuple[str, str]],
    abbreviation_dictionary: List[Tuple[str, str]],
) -> str:
    lines: List[str] = []
    lines.append("【表記優先ルール】")
    lines.append("以下の辞書に載っている表記は、一般的な推定や文脈補完よりも優先して採用してください。")
    lines.append("議事録本文、見出し、要約、Next Action、発言録のすべてで辞書表記を優先してください。")
    lines.append("同じ語が複数回出る場合は、文書全体で辞書の表記に統一してください。")
    lines.append("辞書にない語だけを通常の文脈判断で整えてください。")
    lines.append("")

    lines.append("＜企業名辞書＞")
    if company_dictionary:
        for source, preferred in company_dictionary:
            lines.append(f"- {source} → {preferred}")
    else:
        lines.append("- なし")

    lines.append("")
    lines.append("＜略語辞書＞")
    if abbreviation_dictionary:
        for source, preferred in abbreviation_dictionary:
            lines.append(f"- {source} → {preferred}")
    else:
        lines.append("- なし")

    return "\n".join(lines)


def _normalize_speaker_label(label: Any) -> str:
    text = str(label or "").strip().lower()

    customer_keywords = ["顧客", "お客様", "客先", "customer", "client", "partner"]
    precena_keywords = ["プレセナ", "precena", "弊社", "当社", "自社", "社内"]

    for keyword in customer_keywords:
        if keyword.lower() in text:
            return "顧客"

    for keyword in precena_keywords:
        if keyword.lower() in text:
            return "プレセナ"

    return "プレセナ"


def _parse_labeled_transcript_lines(transcript: str) -> List[Dict[str, str]]:
    parsed: List[Dict[str, str]] = []

    for raw_line in str(transcript or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if ":" in line:
            speaker, text = line.split(":", 1)
        elif "：" in line:
            speaker, text = line.split("：", 1)
        else:
            parsed.append(
                {
                    "speaker": "プレセナ",
                    "text": line,
                }
            )
            continue

        body = text.strip()
        if not body:
            continue

        parsed.append(
            {
                "speaker": _normalize_speaker_label(speaker),
                "text": body,
            }
        )

    return parsed


def _build_labeled_transcript_text(transcript_entries: List[Dict[str, str]]) -> str:
    lines: List[str] = []
    for entry in transcript_entries:
        speaker = _normalize_speaker_label(entry.get("speaker"))
        text = str(entry.get("text", "")).strip()
        if text:
            lines.append(f"{speaker}: {text}")
    return "\n".join(lines)


def _trim_transcript_entries_for_minutes_prompt(
    transcript_entries: List[Dict[str, str]],
    max_lines: int,
) -> List[Dict[str, str]]:
    if max_lines <= 0 or len(transcript_entries) <= max_lines:
        return transcript_entries

    head_count = max_lines // 2
    tail_count = max_lines - head_count

    head_entries = transcript_entries[:head_count]
    tail_entries = transcript_entries[-tail_count:]

    trimmed: List[Dict[str, str]] = []
    trimmed.extend(head_entries)
    trimmed.append(
        {
            "speaker": "プレセナ",
            "text": f"（中略: {len(transcript_entries) - max_lines}発言）",
        }
    )
    trimmed.extend(tail_entries)
    return trimmed


def _build_minutes_prompt(
    transcript: str,
    meeting_info: Dict[str, Any],
    company_dictionary: List[Tuple[str, str]],
    abbreviation_dictionary: List[Tuple[str, str]],
) -> str:
    dictionary_rule_text = _build_dictionary_rule_text(
        company_dictionary=company_dictionary,
        abbreviation_dictionary=abbreviation_dictionary,
    )

    customer_name = meeting_info.get("customer_name", "")
    meeting_title = meeting_info.get("meeting_title", "")
    meeting_date = meeting_info.get("date", "")

    parsed_transcript_entries = _parse_labeled_transcript_lines(transcript)
    prompt_max_lines = int(
        getattr(
            settings,
            "MINUTES_PROMPT_MAX_TRANSCRIPT_LINES",
            _MINUTES_PROMPT_MAX_TRANSCRIPT_LINES,
        )
    )
    prompt_transcript_entries = _trim_transcript_entries_for_minutes_prompt(
        transcript_entries=parsed_transcript_entries,
        max_lines=prompt_max_lines,
    )
    prompt_labeled_transcript_text = _build_labeled_transcript_text(prompt_transcript_entries)

    return f"""あなたは商談・会議の議事録作成担当です。
以下の仮話者ラベル付き文字起こしから、完成済みのMarkdown議事録を作成してください。

会議情報:
- 日付: {meeting_date}
- 顧客名: {customer_name}
- 会議タイトル: {meeting_title}

出力要件:
- 必ずMarkdownで出力する
- 先頭から最後まで、以下の見出しをこの順序で必ず含める
- ## 会議概要
- ## 決まったこと
- ## 残論点
- ## Next Action
- ## 発言録（逐語）
- 推測で事実を足さない
- 会議で実際に話された内容を優先する
- 発言録（逐語）は前処理済み transcript をほぼ全文使用する
- 発言録（逐語）では**要約しない**
- 発言録（逐語）では**削除しない**
- 会議で使われた表現を優先し、迷ったら正式名称を使う
- 文書全体で表記を統一する

{dictionary_rule_text}

入力文字起こしについて:
- 入力はすでに発言単位ごとに区切られている
- 各行の先頭には仮話者ラベルとして「顧客」または「プレセナ」が付いている
- 発言録では、この入力の話者ラベルを維持したまま使う
- 発言録の speaker は必ず「顧客」または「プレセナ」のどちらかだけを使う
- 発言録では、「お客様」「客先」「customer」などの別名に変換しない
- 発言録の text は、入力発言を要約せず、意味を変えず、できるだけ逐語に近く残す
- 話者ラベルが不自然に見えても、入力で与えられた話者ラベルを優先して維持する

重要ルール:
- 企業名、サービス名、製品名、部署名、略語、専門用語は上記辞書の表記を最優先する
- 原文に揺れがあっても最終議事録では辞書表記に統一する
- 要約パートだけでなく、発言録でも辞書表記を優先する
- 辞書表記を優先した結果として一般的な表記と異なっても辞書表記を採用する
- 辞書にない語のみ、文脈に沿って自然な表記に整える
- 発言録では入力行の順序をできるだけ維持する
- 発言録では入力の各発言単位を尊重し、不必要な統合や分割をしない
- 発言録（逐語）では、入力 transcript を要約しない
- 発言録（逐語）では、入力 transcript を可能な限り全文残す
- 発言録（逐語）では、同一発言の重複だけ除去してよい
- 発言録（逐語）では、重複以外の発言は削除しない
- 話者は「顧客」「プレセナ」で整理する

Markdown形式の例:
## 会議概要
...

## 決まったこと
- ...

## 残論点
- ...

## Next Action
- ...

## 発言録（逐語）
- 顧客: ...
- プレセナ: ...

仮話者ラベル付き文字起こし:
<<<TRANSCRIPT
{prompt_labeled_transcript_text}
TRANSCRIPT>>>
"""


_REVIEW_FOLLOWUP_START = "<<<FOLLOWUP_QUESTION>>>"
_REVIEW_FOLLOWUP_END = "<<<END_FOLLOWUP_QUESTION>>>"
_REVIEW_DISALLOWED_MULTI_TARGET_TOKENS = ["/", "・", "、", ",", "および", "及び"]
_REVIEW_GENERIC_TARGET_WORDS = {
    "それ",
    "あれ",
    "これ",
    "この件",
    "その件",
    "あの件",
    "内容",
    "件",
}


def _is_valid_review_single_target(target: str) -> bool:
    cleaned = str(target or "").strip()
    if not cleaned:
        return False
    if any(token in cleaned for token in _REVIEW_DISALLOWED_MULTI_TARGET_TOKENS):
        return False
    if cleaned in _REVIEW_GENERIC_TARGET_WORDS:
        return False

    has_ascii_term = bool(re.search(r"[A-Za-z0-9]{2,}", cleaned))
    has_japanese_term = bool(re.search(r"[一-龯ァ-ヶぁ-ん]{2,}", cleaned))
    return has_ascii_term or has_japanese_term


def _normalize_review_followup_question(raw_text: str) -> str:
    text = str(raw_text or "").strip()
    if not text or text == "（質問文 or 空欄）":
        return ""

    normalized = text.replace("\r", " ").replace("\n", " ")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    normalized = re.split(r"(?<=[。！？?!])\s+", normalized)[0].strip()
    normalized = normalized.rstrip("。 ")

    if any(pattern in normalized for pattern in ["特定できますか", "説明はありますか", "整理できますか"]):
        return ""

    m1 = re.fullmatch(r"(.+?)はこの会話内で定義されていますか[？?]?", normalized)
    m2 = re.fullmatch(r"(.+?)とはこの文脈で何を指していますか[？?]?", normalized)
    if not m1 and not m2:
        return ""

    target = (m1.group(1) if m1 else m2.group(1)).strip()
    if not _is_valid_review_single_target(target):
        return ""

    if m1:
        return f"{target}はこの会話内で定義されていますか？"
    return f"{target}とはこの文脈で何を指していますか？"


def _build_review_prompt(minutes_text: str) -> str:
    return f"""あなたは議事録品質レビュー担当です。
以下のMarkdown議事録を、品質基準に従って見直し、修正済みの完全な議事録をMarkdownで出力してください。

【品質レビュー基準】

1. 固有名詞の正規化
   - 人名の表記ゆれを統一する（例：秋元 / 秋本 → どちらかに統一）
   - 同一人物の複数表記を禁止する
   - 不明な場合はそのまま残す（推測による補完・変更禁止）

2. 決定事項の精査
   - 「## 決まったこと」には、会議で明確に合意された内容のみ残す
   - 仮説・推測・未確定の内容は「## 決まったこと」から除外し、「## 残論点」へ移動する

3. Next Actionの具体化
   - 「## Next Action」の全項目を「誰が / 何をする」が明確な形に修正する
   - NG例：〜を検討する、〜を確認する（担当者が不明なもの）
   - OK例：〇〇さんが△△を作成する、〇〇が□□に連絡する

4. 残論点の明確化
   - 未確定事項を明示する
   - 可能な範囲で「誰が決めるか / 何が不足か」を補足する

5. 構造整合性チェック
   - 以下の区分が矛盾なく整合していること
     - 会議概要
     - 決まったこと
     - 残論点
     - Next Action
   - 矛盾がある場合は修正する

【出力要件】
- 元の議事録と同じMarkdownフォーマットを維持すること
- セクション見出し（## 会議概要 / ## 決まったこと / ## 残論点 / ## Next Action / ## 発言録（逐語））を変更しないこと
- 「## 発言録（逐語）」セクションの内容は一切変更しないこと（そのまま出力）
- 修正が不要な箇所は元の文言をそのまま維持すること
- フォローアップ質問を議事録本文に埋め込まないこと
- コードブロックや前置き文章は不要

【フォローアップ質問の生成基準】
以下の条件に該当する箇所がある場合のみ最大1問だけ出力すること。
それ以外の理由では質問しないこと。

質問できる条件（発言録内の意味不成立のみ）:
- 指示語・代名詞の参照先が発言録を読んでも特定不能
- 主語・発言主体が発言録から読み取れない
- 固有名詞の同一性が発言録中で不明
- 前後を読んでも文脈として意味が通らない箇所

質問できない条件（完全禁止）:
- Next Action補完・担当者確認
- 決定事項の補完
- 議事録の構造整備のための質問
- 発言録に出ていない情報の補完
- 「特定できますか？」「説明はありますか？」「整理できますか？」などのメタ質問

【出力フォーマット】
まず修正済みのMarkdown議事録本文のみを出力してください。
その後、以下の区切り行でフォローアップ質問を出力してください。
上記条件に該当する箇所があれば最大1問。なければ区切り行の中を空欄にしてください。

質問文の形式は必ず次のどちらかだけを使うこと。
- 「◯◯はこの会話内で定義されていますか？」
- 「◯◯とはこの文脈で何を指していますか？」

制約:
- 1質問 = 1対象（複数概念を同時に扱わない）
- ◯◯には具体名詞（例: TPM / 湯間さん / THR）を1つ入れる

<<<FOLLOWUP_QUESTION>>>
（質問文 or 空欄）
<<<END_FOLLOWUP_QUESTION>>>

入力議事録:
<<<MINUTES
{minutes_text}
MINUTES>>>
"""


def _build_verbatim_transcript_lines(transcript: str) -> List[str]:
    lines = [line.strip() for line in str(transcript or "").splitlines() if line.strip()]
    if not lines:
        return ["- なし"]
    return [f"- {line}" for line in lines]


def _extract_section_body(markdown_text: str, heading: str) -> str:
    pattern = rf"{re.escape(heading)}\s*\n([\s\S]*?)(?=\n##\s|\Z)"
    matched = re.search(pattern, markdown_text)
    if not matched:
        return ""
    return matched.group(1).strip()


def _normalize_markdown_minutes(markdown_text: str, transcript: str) -> str:
    text = str(markdown_text or "").strip()
    text = text.replace("## 発言録（ほぼ逐語）", "## 発言録（逐語）")
    text = text.replace("## 発言録\n", "## 発言録（逐語）\n")

    summary_body = _extract_section_body(text, "## 会議概要")
    decisions_body = _extract_section_body(text, "## 決まったこと")
    issues_body = _extract_section_body(text, "## 残論点")
    next_action_body = _extract_section_body(text, "## Next Action")
    utterances_body = _extract_section_body(text, "## 発言録（逐語）")

    transcript_lines = _build_verbatim_transcript_lines(transcript)

    if not decisions_body:
        decisions_body = "- なし"
    if not issues_body:
        issues_body = "- なし"
    if not next_action_body:
        next_action_body = "- なし"

    existing_lines = [line.strip() for line in utterances_body.splitlines() if line.strip()]
    if not existing_lines or existing_lines == ["- なし"]:
        utterance_lines = transcript_lines
    else:
        merged_lines = existing_lines[:]
        for line in transcript_lines:
            if line not in merged_lines:
                merged_lines.append(line)
        utterance_lines = merged_lines

    return "\n".join(
        [
            "## 会議概要",
            summary_body,
            "",
            "## 決まったこと",
            decisions_body,
            "",
            "## 残論点",
            issues_body,
            "",
            "## Next Action",
            next_action_body,
            "",
            "## 発言録（逐語）",
            *utterance_lines,
        ]
    ).strip()


def _extract_text_from_response(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if output_text:
        return str(output_text).strip()

    content = getattr(response, "content", None)
    if not content:
        raise ValueError("Minutes generation response content is empty.")

    texts: List[str] = []
    for block in content:
        text_value = getattr(block, "text", None)
        if text_value:
            texts.append(text_value)

    result = "\n".join(texts).strip()
    if not result:
        raise ValueError("Minutes generation response text is empty.")

    return result


def _parse_minutes_json(text: str) -> Dict[str, Any]:
    normalized_text = str(text or "").strip()

    if normalized_text.startswith("```"):
        normalized_text = normalized_text.strip("`")
        normalized_text = normalized_text.replace("json\n", "", 1).strip()

    try:
        return json.loads(normalized_text)
    except json.JSONDecodeError:
        start = normalized_text.find("{")
        end = normalized_text.rfind("}")
        if start != -1 and end != -1 and start < end:
            candidate = normalized_text[start : end + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                open_braces = candidate.count("{")
                close_braces = candidate.count("}")
                if open_braces > close_braces:
                    candidate = candidate + ("}" * (open_braces - close_braces))
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    pass

        logger.warning("Minutes generation returned invalid JSON. Using safe fallback payload.")
        return {
            "summary": normalized_text,
            "decisions": [],
            "issues": [],
            "next_actions": [],
            "utterances": [],
        }


def _normalize_transcript_entries(entries: Any) -> List[Dict[str, str]]:
    normalized: List[Dict[str, str]] = []

    if not isinstance(entries, list):
        return normalized

    for entry in entries:
        if isinstance(entry, dict):
            speaker = _normalize_speaker_label(entry.get("speaker"))
            text = str(entry.get("text", "")).strip()
            if text:
                normalized.append({"speaker": speaker, "text": text})
        elif isinstance(entry, str) and entry.strip():
            parsed_lines = _parse_labeled_transcript_lines(entry)
            normalized.extend(parsed_lines)

    return normalized


def _normalize_minutes_payload(minutes_json: Dict[str, Any]) -> Dict[str, Any]:
    next_actions = minutes_json.get("next_actions") or minutes_json.get("Next Action") or []
    normalized_actions: List[Dict[str, str]] = []
    if isinstance(next_actions, list):
        for action in next_actions:
            if isinstance(action, dict):
                normalized_actions.append(
                    {
                        "task": str(action.get("task", "")).strip(),
                        "owner": _normalize_speaker_label(action.get("owner")),
                        "due": str(action.get("due", "")).strip(),
                    }
                )
            elif str(action).strip():
                normalized_actions.append(
                    {
                        "task": str(action).strip(),
                        "owner": "プレセナ",
                        "due": "",
                    }
                )

    utterances = _normalize_transcript_entries(
        minutes_json.get("utterances")
        or minutes_json.get("transcript")
        or minutes_json.get("発言録")
        or []
    )

    return {
        "summary": str(
            minutes_json.get("summary")
            or minutes_json.get("meeting_summary")
            or minutes_json.get("会議概要")
            or ""
        ).strip(),
        "decisions": list(minutes_json.get("decisions") or minutes_json.get("決まったこと") or []),
        "issues": list(minutes_json.get("issues") or minutes_json.get("open_points") or minutes_json.get("残論点") or []),
        "next_actions": normalized_actions,
        "utterances": utterances,
    }


def generate_minutes_with_claude(
    transcript: str,
    meeting_info: Dict[str, Any],
    company_dictionary: Any = None,
    abbreviation_dictionary: Any = None,
) -> str:
    if not transcript or not transcript.strip():
        logger.warning("Minutes generation skipped because transcript is empty. Using fallback markdown.")
        return "\n".join(
            [
                "## 会議概要",
                "文字起こし結果が空のため議事録を生成できませんでした。",
                "",
                "## 決まったこと",
                "- なし",
                "",
                "## 残論点",
                "- なし",
                "",
                "## Next Action",
                "- なし",
                "",
                "## 発言録（逐語）",
                "- なし",
            ]
        )

    if company_dictionary is None:
        company_dictionary = load_company_dictionary()
    if abbreviation_dictionary is None:
        abbreviation_dictionary = load_abbreviation_dictionary()

    normalized_company_dictionary = _normalize_dictionary_items(company_dictionary)
    normalized_abbreviation_dictionary = _normalize_dictionary_items(abbreviation_dictionary)

    prompt = _build_minutes_prompt(
        transcript=transcript,
        meeting_info=meeting_info,
        company_dictionary=normalized_company_dictionary,
        abbreviation_dictionary=normalized_abbreviation_dictionary,
    )

    transcript_line_count = len([line for line in str(transcript or "").splitlines() if line.strip()])
    prompt_max_lines = int(
        getattr(
            settings,
            "MINUTES_PROMPT_MAX_TRANSCRIPT_LINES",
            _MINUTES_PROMPT_MAX_TRANSCRIPT_LINES,
        )
    )
    logger.info(
        "MINUTES_PROMPT_INPUT: transcript_lines=%s prompt_max_lines=%s",
        transcript_line_count,
        prompt_max_lines,
    )

    model = getattr(settings, "OPENAI_GPT_PREPROCESS_MODEL", "gpt-4.1-mini")
    max_tokens = int(getattr(settings, "CLAUDE_MAX_TOKENS", 4000))
    temperature = float(getattr(settings, "CLAUDE_TEMPERATURE", 0))

    logger.info(
        "Start OpenAI minutes generation. company_dict=%s abbreviation_dict=%s model=%s",
        len(normalized_company_dictionary),
        len(normalized_abbreviation_dictionary),
        model,
    )

    client = OpenAI(api_key=settings.OPENAI_API_KEY)

    try:
        response = client.responses.create(
            model=model,
            temperature=temperature,
            max_output_tokens=max_tokens,
            input=prompt,
        )
        response_text = _extract_text_from_response(response)
        return _normalize_markdown_minutes(str(response_text).strip(), transcript)
    except Exception as e:
        error_text = str(e)
        logger.warning("Minutes generation API error detail: %s", error_text)
        if (
            "authentication_error" in error_text
            or "invalid x-api-key" in error_text.lower()
            or "401" in error_text
            or "not_found_error" in error_text
            or "model not found" in error_text.lower()
            or "404" in error_text
        ):
            logger.warning(
                "Minutes generation API failed with auth/model error (401/404). Return fallback minutes JSON. error=%s",
                error_text,
            )
            return "\n".join(
                [
                    "## 会議概要",
                    "[MINUTES_GENERATION_SKIPPED_CLAUDE_AUTH_ERROR]",
                    "",
                    "## 決まったこと",
                    "- なし",
                    "",
                    "## 残論点",
                    "- なし",
                    "",
                    "## Next Action",
                    "- なし",
                    "",
                    "## 発言録（逐語）",
                    *_build_verbatim_transcript_lines(transcript),
                ]
            )
        logger.exception("OpenAI minutes generation failed: %s", e)
        raise


def review_minutes(minutes_text: str) -> Tuple[str, str]:
    """議事録の品質レビューを1回実行し、(修正済み議事録, followup_question) を返す。失敗時は (元テキスト, "") を返す。"""
    logger.info("=== REVIEW START ===")
    before_len = len(minutes_text)

    model = getattr(settings, "OPENAI_GPT_PREPROCESS_MODEL", "gpt-4.1-mini")
    max_tokens = int(getattr(settings, "CLAUDE_MAX_TOKENS", 4000))
    temperature = float(getattr(settings, "CLAUDE_TEMPERATURE", 0))

    prompt = _build_review_prompt(minutes_text)
    client = OpenAI(api_key=settings.OPENAI_API_KEY)

    try:
        response = client.responses.create(
            model=model,
            temperature=temperature,
            max_output_tokens=max_tokens,
            input=prompt,
        )
        raw_text = _extract_text_from_response(response).strip()

        if _REVIEW_FOLLOWUP_START in raw_text and _REVIEW_FOLLOWUP_END in raw_text:
            parts = raw_text.split(_REVIEW_FOLLOWUP_START, 1)
            reviewed_text = parts[0].strip()
            followup_raw = parts[1].split(_REVIEW_FOLLOWUP_END, 1)[0].strip()
            followup_question = _normalize_review_followup_question(followup_raw)
        else:
            reviewed_text = raw_text
            followup_question = ""

        after_len = len(reviewed_text)
        logger.info("REVIEW_APPLIED: true")
        logger.info("REVIEW_DIFF: before=%s after=%s", before_len, after_len)
        logger.info("REVIEW_FOLLOWUP_PRESENT: %s", bool(followup_question))
        return reviewed_text, followup_question
    except Exception as exc:
        logger.warning("REVIEW_FAILED: reason=%s", exc)
        logger.info("REVIEW_APPLIED: false (fallback to original)")
        return minutes_text, ""
