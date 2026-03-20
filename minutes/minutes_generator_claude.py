import json
import re
from typing import Any, Dict, List, Optional, Tuple

from openai import OpenAI

from config.settings import settings
from dictionary.abbreviation_dictionary import load_abbreviation_dictionary
from dictionary.company_dictionary import load_company_dictionary
from utils.logger import get_logger

logger = get_logger(__name__)

_MINUTES_PROMPT_MAX_TRANSCRIPT_LINES = 400
_MIN_VERBATIM_LINES_FOR_CONFIDENCE = 3
_MINUTES_CHUNK_MAX_LINES = 180
_MINUTES_CHUNK_MAX_CHARS = 6000


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
    prompt_max_lines_override: Optional[int] = None,
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
        prompt_max_lines_override
        if prompt_max_lines_override is not None
        else getattr(
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
- ## 発言録（逐語）
- ## 会議概要
- ## 決まったこと
- ## 残論点
- ## Next Action
- 推測で事実を足さない
- 会議で実際に話された内容を優先する
- 発言録（逐語）は前処理済み transcript をほぼ全文使用する
- 発言録（逐語）では**要約しない**
- 発言録（逐語）では**削除しない**
- 発言録（逐語）を根拠データとし、後続項目はその派生情報として作成する
- 決まったこと/残論点/Next Action は、発言録（逐語）に根拠がある内容だけを書く
- 根拠が発言録に見当たらない内容は書かない
- 発言録（逐語）が短い・不完全な場合は、会議概要の先頭に注意書きを入れ、後続項目の確定度を下げる
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
## 発言録（逐語）
- 顧客: ...
- プレセナ: ...

## 会議概要
...

## 決まったこと
- ...

## 残論点
- ...

## Next Action
- ...

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
    "それぞれ",
    "そのあたり",
    "このあたり",
    "あのあたり",
    "内容",
    "件",
}


def _normalize_review_question_target_text(target: str) -> str:
    text = str(target or "").strip()
    if not text:
        return ""

    text = re.sub(r"^[\s\"'「『（(【\[]+", "", text)
    text = re.sub(r"[\s\"'」』）)】\]]+$", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _is_valid_review_single_target(target: str) -> bool:
    cleaned = _normalize_review_question_target_text(target)
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
         - 発言録（逐語）
     - 会議概要
     - 決まったこと
     - 残論点
     - Next Action
   - 矛盾がある場合は修正する

【出力要件】
- 元の議事録と同じMarkdownフォーマットを維持すること
- セクション見出し（## 発言録（逐語） / ## 会議概要 / ## 決まったこと / ## 残論点 / ## Next Action）を変更しないこと
- セクションの順序は必ず次の順に固定すること（発言録が先頭）
    1) 発言録（逐語）
    2) 会議概要
    3) 決まったこと
    4) 残論点
    5) Next Action
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
    transcript_line_count = len([line for line in str(transcript or "").splitlines() if line.strip()])

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

    summary_lines = [line for line in summary_body.splitlines() if line.strip()]
    if transcript_line_count <= _MIN_VERBATIM_LINES_FOR_CONFIDENCE:
        low_confidence_warning = "- ⚠ 発言録（逐語）が短いため、後続項目は確定度低（要確認）"
        if low_confidence_warning not in summary_lines:
            summary_lines.insert(0, low_confidence_warning)
        summary_body = "\n".join(summary_lines).strip()
        logger.warning(
            "MINUTES_LOW_CONFIDENCE_VERBATIM: transcript_lines=%s threshold=%s",
            transcript_line_count,
            _MIN_VERBATIM_LINES_FOR_CONFIDENCE,
        )

    return "\n".join(
        [
            "## 発言録（逐語）",
            *utterance_lines,
            "",
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


def _is_incomplete_due_to_max_tokens(response: Any) -> bool:
    details = getattr(response, "incomplete_details", None)
    if details is None:
        return False

    reason = ""
    if isinstance(details, dict):
        reason = str(details.get("reason") or "").strip().lower()
    else:
        reason = str(getattr(details, "reason", "") or "").strip().lower()

    return reason == "max_output_tokens"


def _response_status_name(response: Any) -> str:
    return str(getattr(response, "status", "") or "").strip().lower()


def _extract_usage_input_tokens(response: Any) -> Optional[int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None

    if isinstance(usage, dict):
        value = usage.get("input_tokens")
    else:
        value = getattr(usage, "input_tokens", None)

    if value is None:
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _estimate_text_tokens(text: str) -> int:
    # Rough fallback when tokenizer metadata is unavailable.
    return max(1, (len(str(text or "")) + 3) // 4)


def _is_incomplete_response(response: Any) -> bool:
    status = _response_status_name(response)
    return status == "incomplete" or _is_incomplete_due_to_max_tokens(response)


def _missing_required_sections(markdown_text: str) -> List[str]:
    text = str(markdown_text or "")
    required = [
        "## 発言録（逐語）",
        "## 会議概要",
        "## 決まったこと",
        "## 残論点",
        "## Next Action",
    ]
    return [section for section in required if section not in text]


def _build_balanced_two_part_transcripts(transcript: str) -> Tuple[str, str]:
    entries = _parse_labeled_transcript_lines(transcript)
    if len(entries) <= 1:
        single = _build_labeled_transcript_text(entries)
        return single, single

    mid = max(1, len(entries) // 2)
    part1 = _build_labeled_transcript_text(entries[:mid])
    part2 = _build_labeled_transcript_text(entries[mid:])
    return part1, part2


def _split_transcript_entries_for_minutes(
    transcript_entries: List[Dict[str, str]],
    max_lines_per_chunk: int,
    max_chars_per_chunk: int,
) -> List[List[Dict[str, str]]]:
    if not transcript_entries:
        return []

    safe_max_lines = max(20, int(max_lines_per_chunk or _MINUTES_CHUNK_MAX_LINES))
    safe_max_chars = max(1200, int(max_chars_per_chunk or _MINUTES_CHUNK_MAX_CHARS))

    chunks: List[List[Dict[str, str]]] = []
    current_chunk: List[Dict[str, str]] = []
    current_chars = 0

    for entry in transcript_entries:
        speaker = _normalize_speaker_label(entry.get("speaker"))
        text = str(entry.get("text", "")).strip()
        if not text:
            continue

        normalized_entry = {"speaker": speaker, "text": text}
        estimated_chars = len(speaker) + len(text) + 3

        exceeds_lines = len(current_chunk) >= safe_max_lines
        exceeds_chars = current_chars + estimated_chars > safe_max_chars
        if current_chunk and (exceeds_lines or exceeds_chars):
            chunks.append(current_chunk)
            current_chunk = []
            current_chars = 0

        current_chunk.append(normalized_entry)
        current_chars += estimated_chars

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def _merge_chunk_minutes_markdown(
    chunk_markdowns: List[str],
    transcript: str,
) -> str:
    summary_blocks: List[str] = []
    decision_lines: List[str] = []
    issue_lines: List[str] = []
    action_lines: List[str] = []

    def _append_unique(target: List[str], lines: List[str]) -> None:
        for line in lines:
            normalized = str(line or "").strip()
            if not normalized:
                continue
            if normalized not in target:
                target.append(normalized)

    for markdown_text in chunk_markdowns:
        summary_body = _extract_section_body(markdown_text, "## 会議概要")
        if summary_body:
            summary_blocks.append(summary_body)

        decisions_body = _extract_section_body(markdown_text, "## 決まったこと")
        issues_body = _extract_section_body(markdown_text, "## 残論点")
        actions_body = _extract_section_body(markdown_text, "## Next Action")

        _append_unique(decision_lines, [line for line in decisions_body.splitlines() if line.strip()])
        _append_unique(issue_lines, [line for line in issues_body.splitlines() if line.strip()])
        _append_unique(action_lines, [line for line in actions_body.splitlines() if line.strip()])

    summary_body_merged = "\n\n".join([block for block in summary_blocks if block.strip()]).strip() or "- なし"
    decisions_body_merged = "\n".join(decision_lines).strip() or "- なし"
    issues_body_merged = "\n".join(issue_lines).strip() or "- なし"
    actions_body_merged = "\n".join(action_lines).strip() or "- なし"

    raw_markdown = "\n".join(
        [
            "## 会議概要",
            summary_body_merged,
            "",
            "## 決まったこと",
            decisions_body_merged,
            "",
            "## 残論点",
            issues_body_merged,
            "",
            "## Next Action",
            actions_body_merged,
        ]
    ).strip()
    return _normalize_markdown_minutes(raw_markdown, transcript)


def _generate_minutes_once(
    *,
    client: OpenAI,
    model: str,
    temperature: float,
    max_tokens: int,
    transcript: str,
    meeting_info: Dict[str, Any],
    company_dictionary: List[Tuple[str, str]],
    abbreviation_dictionary: List[Tuple[str, str]],
    phase_label: str,
) -> Tuple[str, bool]:
    prompt = _build_minutes_prompt(
        transcript=transcript,
        meeting_info=meeting_info,
        company_dictionary=company_dictionary,
        abbreviation_dictionary=abbreviation_dictionary,
        prompt_max_lines_override=100000,
    )
    prompt_chars = len(prompt)
    prompt_lines = len([line for line in prompt.splitlines() if line.strip()])
    estimated_input_tokens = _estimate_text_tokens(prompt)

    logger.info(
        "MINUTES_REQUEST: phase=%s prompt_chars=%s prompt_lines=%s estimated_input_tokens=%s max_output_tokens=%s",
        phase_label,
        prompt_chars,
        prompt_lines,
        estimated_input_tokens,
        max_tokens,
    )

    response = client.responses.create(
        model=model,
        temperature=temperature,
        max_output_tokens=max_tokens,
        input=prompt,
    )
    usage_input_tokens = _extract_usage_input_tokens(response)
    logger.info(
        "MINUTES_RESPONSE: phase=%s status=%s usage_input_tokens=%s output_text_chars=%s",
        phase_label,
        getattr(response, "status", ""),
        usage_input_tokens if usage_input_tokens is not None else "unknown",
        len(str(getattr(response, "output_text", "") or "")),
    )
    is_incomplete = _is_incomplete_response(response)
    if is_incomplete:
        logger.warning(
            "MINUTES_RESPONSE_INCOMPLETE: phase=%s status=%s usage_input_tokens=%s prompt_chars=%s prompt_lines=%s estimated_input_tokens=%s",
            phase_label,
            getattr(response, "status", ""),
            usage_input_tokens if usage_input_tokens is not None else "unknown",
            prompt_chars,
            prompt_lines,
            estimated_input_tokens,
        )

    response_text = _extract_text_from_response(response)
    normalized = _normalize_markdown_minutes(str(response_text).strip(), transcript)
    return normalized, is_incomplete


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
    chunk_max_lines = int(getattr(settings, "MINUTES_CHUNK_MAX_LINES", _MINUTES_CHUNK_MAX_LINES))
    chunk_max_chars = int(getattr(settings, "MINUTES_CHUNK_MAX_CHARS", _MINUTES_CHUNK_MAX_CHARS))

    logger.info(
        "Start OpenAI minutes generation. company_dict=%s abbreviation_dict=%s model=%s max_output_tokens=%s",
        len(normalized_company_dictionary),
        len(normalized_abbreviation_dictionary),
        model,
        max_tokens,
    )

    client = OpenAI(api_key=settings.OPENAI_API_KEY)

    parsed_transcript_entries = _parse_labeled_transcript_lines(transcript)
    transcript_chunks = _split_transcript_entries_for_minutes(
        transcript_entries=parsed_transcript_entries,
        max_lines_per_chunk=chunk_max_lines,
        max_chars_per_chunk=chunk_max_chars,
    )
    if not transcript_chunks:
        transcript_chunks = [parsed_transcript_entries]

    logger.info(
        "TRANSCRIPT_CHUNK_PLAN: total_entries=%s chunks=%s chunk_max_lines=%s chunk_max_chars=%s",
        len(parsed_transcript_entries),
        len(transcript_chunks),
        chunk_max_lines,
        chunk_max_chars,
    )

    try:
        chunk_total = len(transcript_chunks)
        collected_chunk_transcripts: List[str] = []

        for idx, chunk_entries in enumerate(transcript_chunks, start=1):
            chunk_transcript = _build_labeled_transcript_text(chunk_entries)
            chunk_line_count = len(chunk_entries)
            logger.info(
                "TRANSCRIPT_CHUNK_DONE: chunk=%s/%s transcript_lines=%s transcript_chars=%s",
                idx,
                chunk_total,
                chunk_line_count,
                len(chunk_transcript),
            )
            collected_chunk_transcripts.append(chunk_transcript)

        if len(collected_chunk_transcripts) != chunk_total:
            raise RuntimeError(
                f"Transcript chunk processing mismatch: expected={chunk_total} actual={len(collected_chunk_transcripts)}"
            )

        merged_transcript = "\n".join(
            [chunk_text.strip() for chunk_text in collected_chunk_transcripts if chunk_text.strip()]
        ).strip()
        if not merged_transcript:
            raise RuntimeError("Merged transcript is empty after chunk collection.")
        logger.info(
            "TRANSCRIPT_MERGE_DONE: chunks=%s merged_chars=%s",
            chunk_total,
            len(merged_transcript),
        )

        minutes_markdown, is_incomplete = _generate_minutes_once(
            client=client,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            transcript=merged_transcript,
            meeting_info=meeting_info,
            company_dictionary=normalized_company_dictionary,
            abbreviation_dictionary=normalized_abbreviation_dictionary,
            phase_label="final_single_pass",
        )

        final_markdown = minutes_markdown
        if is_incomplete:
            logger.warning(
                "MINUTES_FINAL_INCOMPLETE_DETECTED: action=repartition_once transcript_chars=%s",
                len(merged_transcript),
            )
            part1_transcript, part2_transcript = _build_balanced_two_part_transcripts(merged_transcript)
            if not part1_transcript.strip() or not part2_transcript.strip():
                raise RuntimeError("Transcript repartition failed: one of the parts is empty.")

            logger.info(
                "MINUTES_FINAL_REPARTITION_START: part1_chars=%s part2_chars=%s",
                len(part1_transcript),
                len(part2_transcript),
            )
            part1_markdown, part1_incomplete = _generate_minutes_once(
                client=client,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                transcript=part1_transcript,
                meeting_info=meeting_info,
                company_dictionary=normalized_company_dictionary,
                abbreviation_dictionary=normalized_abbreviation_dictionary,
                phase_label="repartition_part1",
            )
            if part1_incomplete:
                raise RuntimeError("Minutes repartition failed: part1 still incomplete.")

            part2_markdown, part2_incomplete = _generate_minutes_once(
                client=client,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                transcript=part2_transcript,
                meeting_info=meeting_info,
                company_dictionary=normalized_company_dictionary,
                abbreviation_dictionary=normalized_abbreviation_dictionary,
                phase_label="repartition_part2",
            )
            if part2_incomplete:
                raise RuntimeError("Minutes repartition failed: part2 still incomplete.")

            logger.info("MINUTES_FINAL_REPARTITION_PART_DONE: part=1")
            logger.info("MINUTES_FINAL_REPARTITION_PART_DONE: part=2")
            final_markdown = _merge_chunk_minutes_markdown(
                chunk_markdowns=[part1_markdown, part2_markdown],
                transcript=merged_transcript,
            )
            logger.info(
                "MINUTES_FINAL_REPARTITION_MERGED: final_chars=%s",
                len(final_markdown),
            )

        missing_sections = _missing_required_sections(final_markdown)
        if missing_sections:
            raise RuntimeError(f"Minutes structure validation failed: missing={','.join(missing_sections)}")
        logger.info(
            "MINUTES_STRUCTURE_VALIDATION_OK: required_sections=%s",
            5,
        )
        return final_markdown
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
