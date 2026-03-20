import re
from typing import Any, Dict, List, Optional, Tuple

from openai import OpenAI

from config.settings import settings
from dictionary.abbreviation_dictionary import load_abbreviation_dictionary
from dictionary.company_dictionary import load_company_dictionary
from preprocess.transcript_cleaner import clean_transcript
from utils.logger import get_logger

logger = get_logger(__name__)

_GPT_PREPROCESS_CHUNK_SIZE = 80


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
    lines.append("以下の辞書に載っている表記は、一般的な推定や音の近さよりも優先して採用してください。")
    lines.append("辞書に一致・類推できるものは辞書表記へ優先補正してください。")
    lines.append("同じ語が複数回出る場合は、文書全体で辞書の表記に統一してください。")
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


def _build_numbered_utterances(lines: List[str]) -> str:
    return "\n".join(f"{idx + 1}. {line}" for idx, line in enumerate(lines))


def _build_prompt(
    cleaned_lines: List[str],
    company_dictionary: List[Tuple[str, str]],
    abbreviation_dictionary: List[Tuple[str, str]],
    enable_speaker_labeling: bool,
    speaker_rule_prompt_block: str,
) -> str:
    dictionary_rule_text = _build_dictionary_rule_text(
        company_dictionary=company_dictionary,
        abbreviation_dictionary=abbreviation_dictionary,
    )
    numbered_utterances = _build_numbered_utterances(cleaned_lines)

    speaker_task = (
        "各発言の先頭へ仮話者ラベルとして「顧客」または「プレセナ」を付けてください。"
        if enable_speaker_labeling
        else "各発言の先頭に必ず「プレセナ: 」を付けた形式で返してください。"
    )

    return f"""あなたは会議音声の文字起こし前処理担当です。
以下の発言単位を、発言の順序と単位を絶対に崩さずに整形し、
{speaker_task}

目的:
- クリーニング後の発言単位を維持する
- 誤変換を最小限の範囲で補正する
- 表記ゆれを統一する
- 後段の議事録生成で使いやすい入力へ整える

厳守事項:
- 発言数を増やさない
- 連続して同一内容の発言がある場合のみ、1つに統合してよい
- 発言順を変えない
- 1行を複数行に分割しない
- 各行は必ず「顧客: 」または「プレセナ: 」で始める
- 既存の話者ラベル（顧客 / プレセナ）は維持する
- 話者が断定しづらい場合は、文脈上もっとも自然な方を選ぶ
- 要約しない
- 逐語性を維持し、発言本文を短くしない
- 事実を追加しない
- 出力は各行の整形結果のみ。説明不要

{dictionary_rule_text}

{speaker_rule_prompt_block}

補正方針:
1. 各発言の本文は、意味を変えずに誤字のみを最小限補正する
2. 企業名・サービス名・製品名・略語は辞書表記を最優先する
3. 原文に複数表記が混在していても辞書表記へ統一する
4. 会話文脈から各発言に「顧客」または「プレセナ」の仮話者ラベルを付ける
5. 自社説明・提案・案内・確認依頼は「プレセナ」寄り、顧客側の要望・現状説明・質問は「顧客」寄りで判断する
6. 話者推定に自信が薄くても、必ずどちらか一方を付ける
7. 同一内容の連続発言は1つに統合し、それ以外は統合しない

入力発言一覧:
{numbered_utterances}
"""


def _extract_text_from_response(response: Any) -> str:
    if hasattr(response, "output_text") and response.output_text:
        return response.output_text.strip()

    try:
        content = response.output[0].content
        texts = []
        for part in content:
            text_value = getattr(part, "text", None)
            if text_value:
                texts.append(text_value)
        return "\n".join(texts).strip()
    except Exception:
        pass

    try:
        return response.choices[0].message.content.strip()
    except Exception as e:
        raise ValueError(f"OpenAI response text extraction failed: {e}") from e


def _strip_number_prefix(line: str) -> str:
    return re.sub(r"^\s*\d+[\.\):：\-]\s*", "", line).strip()


def _normalize_labeled_line(line: str) -> Optional[str]:
    text = _strip_number_prefix(str(line).strip())
    if not text:
        return None

    matched = re.match(r"^(顧客|プレセナ)\s*[:：]\s*(.+)$", text)
    if matched:
        speaker = matched.group(1)
        body = matched.group(2).strip()
        if body:
            return f"{speaker}: {body}"
        return None

    body = re.sub(r"^(customer|client)\s*[:：]\s*", "", text, flags=re.IGNORECASE).strip()
    if body != text and body:
        return f"顧客: {body}"

    body = re.sub(r"^(precena|プレセナ|弊社|当社)\s*[:：]\s*", "", text, flags=re.IGNORECASE).strip()
    if body != text and body:
        return f"プレセナ: {body}"

    return None


def _fallback_label_line(line: str, enable_speaker_labeling: bool = True) -> str:
    text = str(line).strip()

    if not enable_speaker_labeling:
        return f"プレセナ: {text}"

    customer_keywords = [
        "御社",
        "当社では",
        "うち",
        "弊社側ではなく",
        "使っています",
        "使っていて",
        "困っていて",
        "したい",
        "ほしい",
        "検討しています",
        "課題",
        "現状",
    ]
    precena_keywords = [
        "弊社",
        "当社",
        "プレセナ",
        "ご提案",
        "ご説明",
        "ご案内",
        "対応します",
        "確認します",
        "共有します",
        "送ります",
        "可能です",
    ]

    for keyword in precena_keywords:
        if keyword in text:
            return f"プレセナ: {text}"

    for keyword in customer_keywords:
        if keyword in text:
            return f"顧客: {text}"

    return f"顧客: {text}"


def _postprocess_labeled_lines(
    raw_text: str,
    cleaned_lines: List[str],
    enable_speaker_labeling: bool,
) -> str:
    response_lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    normalized_lines: List[str] = []

    for line in response_lines:
        normalized = _normalize_labeled_line(line)
        if normalized:
            normalized_lines.append(normalized)

    if len(normalized_lines) != len(cleaned_lines):
        logger.warning(
            "GPT labeled line count mismatch. expected=%s actual=%s fallback_used=%s",
            len(cleaned_lines),
            len(normalized_lines),
            True,
        )
        return "\n".join(
            _fallback_label_line(line, enable_speaker_labeling=enable_speaker_labeling)
            for line in cleaned_lines
        )

    return "\n".join(normalized_lines)


def _chunk_lines(lines: List[str], chunk_size: int) -> List[List[str]]:
    if chunk_size <= 0:
        return [lines]
    return [lines[i : i + chunk_size] for i in range(0, len(lines), chunk_size)]


def preprocess_transcript_with_gpt(
    transcript: str,
    company_dictionary: Any = None,
    abbreviation_dictionary: Any = None,
    enable_speaker_labeling: bool = True,
    speaker_labeling_config: Optional[Dict[str, Any]] = None,
    speaker_rule_prompt_block: str = "",
) -> str:
    if not transcript or not transcript.strip():
        return transcript

    cleaned_transcript = clean_transcript(transcript)
    if not cleaned_transcript:
        return ""

    cleaned_lines = [line.strip() for line in cleaned_transcript.splitlines() if line.strip()]
    if not cleaned_lines:
        return ""

    if company_dictionary is None:
        company_dictionary = load_company_dictionary()
    if abbreviation_dictionary is None:
        abbreviation_dictionary = load_abbreviation_dictionary()

    normalized_company_dictionary = _normalize_dictionary_items(company_dictionary)
    normalized_abbreviation_dictionary = _normalize_dictionary_items(abbreviation_dictionary)

    if not speaker_rule_prompt_block:
        speaker_rule_prompt_block = settings.get_gpt_speaker_rule_prompt_block()

    model = getattr(settings, "GPT_PREPROCESS_MODEL", None) or getattr(
        settings, "OPENAI_GPT_PREPROCESS_MODEL", "gpt-4.1-mini"
    )
    temperature = float(getattr(settings, "GPT_PREPROCESS_TEMPERATURE", 0))
    timeout = float(getattr(settings, "GPT_PREPROCESS_TIMEOUT", 120))
    chunk_size = int(getattr(settings, "GPT_PREPROCESS_CHUNK_SIZE", _GPT_PREPROCESS_CHUNK_SIZE))

    logger.info(
        "Start GPT transcript preprocessing with provisional speaker labels. lines=%s company_dict=%s abbreviation_dict=%s model=%s speaker_labeling=%s config=%s",
        len(cleaned_lines),
        len(normalized_company_dictionary),
        len(normalized_abbreviation_dictionary),
        model,
        enable_speaker_labeling,
        speaker_labeling_config or {},
    )

    client = OpenAI(api_key=settings.OPENAI_API_KEY)

    try:
        processed_chunks: List[str] = []

        for chunk_index, chunk_lines in enumerate(_chunk_lines(cleaned_lines, chunk_size), start=1):
            chunk_prompt = _build_prompt(
                cleaned_lines=chunk_lines,
                company_dictionary=normalized_company_dictionary,
                abbreviation_dictionary=normalized_abbreviation_dictionary,
                enable_speaker_labeling=enable_speaker_labeling,
                speaker_rule_prompt_block=speaker_rule_prompt_block,
            )
            logger.info(
                "GPT_PREPROCESS_CHUNK_START: chunk=%s chunk_lines=%s total_lines=%s chunk_size=%s",
                chunk_index,
                len(chunk_lines),
                len(cleaned_lines),
                chunk_size,
            )

            try:
                response = client.responses.create(
                    model=model,
                    temperature=temperature,
                    input=chunk_prompt,
                    timeout=timeout,
                )
                raw_output = _extract_text_from_response(response)
                processed_output = _postprocess_labeled_lines(
                    raw_output,
                    chunk_lines,
                    enable_speaker_labeling=enable_speaker_labeling,
                )
                processed_chunks.append(
                    processed_output or "\n".join(
                        _fallback_label_line(line, enable_speaker_labeling=enable_speaker_labeling)
                        for line in chunk_lines
                    )
                )
            except Exception as e:
                error_text = str(e)
                logger.exception(
                    "GPT_PREPROCESS_EXCEPTION: model=%s lines=%s timeout=%ss error_type=%s chunk=%s chunk_lines=%s",
                    model,
                    len(cleaned_lines),
                    timeout,
                    type(e).__name__,
                    chunk_index,
                    len(chunk_lines),
                )
                if "timeout" in error_text.lower():
                    logger.warning(
                        "GPT_PREPROCESS_TIMEOUT_FALLBACK: continuing with fallback speaker labeling chunk=%s",
                        chunk_index,
                    )
                else:
                    logger.warning(
                        "GPT_PREPROCESS_API_ERROR_FALLBACK: continuing with fallback speaker labeling due to API error chunk=%s",
                        chunk_index,
                    )
                processed_chunks.append(
                    "\n".join(
                        _fallback_label_line(line, enable_speaker_labeling=enable_speaker_labeling)
                        for line in chunk_lines
                    )
                )

        return "\n".join(processed_chunks)
    except Exception as e:
        error_text = str(e)
        logger.exception(
            "GPT_PREPROCESS_EXCEPTION: model=%s lines=%s timeout=%ss error_type=%s",
            model,
            len(cleaned_lines),
            timeout,
            type(e).__name__,
        )
        if "timeout" in error_text.lower():
            logger.warning("GPT_PREPROCESS_TIMEOUT_FALLBACK: continuing with fallback speaker labeling")
            return "\n".join(
                _fallback_label_line(line, enable_speaker_labeling=enable_speaker_labeling)
                for line in cleaned_lines
            )
        logger.warning("GPT_PREPROCESS_API_ERROR_FALLBACK: continuing with fallback speaker labeling due to API error")
        return "\n".join(
            _fallback_label_line(line, enable_speaker_labeling=enable_speaker_labeling)
            for line in cleaned_lines
        )


def preprocess_transcript(transcript: str) -> str:
    return preprocess_transcript_with_gpt(transcript)


def extract_ambiguity_questions_from_preprocessed_text(
    preprocessed_text: str,
    max_questions: int = 3,
) -> List[str]:
    text = str(preprocessed_text or "").strip()
    if not text:
        return []

    questions: List[str] = []
    lowered = text.lower()

    customer_ambiguous_keywords = ["御社", "貴社", "先方", "お客様", "顧客", "クライアント", "相手先"]
    has_customer_hint = any(keyword in text for keyword in customer_ambiguous_keywords)
    has_named_customer = bool(
        re.search(r"(?:株式会社|有限会社|合同会社|Inc\.|Ltd\.|Co\.)", text)
        or re.search(r"[\u30A1-\u30FA]{2,}(?:社|様)", text)
    )
    if has_customer_hint and not has_named_customer:
        questions.append("正式な顧客名は何ですか？")

    action_lines = [line.strip() for line in text.splitlines() if line.strip()]
    action_ambiguous_markers = ["対応します", "確認します", "共有します", "進めます", "検討します"]
    has_action_without_owner = False
    for line in action_lines:
        if not any(marker in line for marker in action_ambiguous_markers):
            continue
        has_owner = any(owner in line for owner in ["顧客", "プレセナ", "当社", "弊社", "先方"])
        has_due = bool(re.search(r"\d{1,2}月\d{1,2}日|\d{4}[-/]\d{1,2}[-/]\d{1,2}|まで", line))
        if not has_owner or not has_due:
            has_action_without_owner = True
            break
    if has_action_without_owner:
        questions.append("Next Actionの担当者と期限は何ですか？")

    suspicious_proper_noun_patterns = [
        r"\[TRANSCRIPTION_[^\]]+\]",
        r"\?{2,}",
        r"不明",
        r"聞き取[れり]",
        r"[○◯]{2,}",
    ]
    has_suspicious_proper_noun = any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in suspicious_proper_noun_patterns)
    if has_suspicious_proper_noun or "skipped_no_quota" in lowered:
        questions.append("不明な固有名詞の正しい表記は何ですか？")

    deduped_questions: List[str] = []
    for question in questions:
        if question not in deduped_questions:
            deduped_questions.append(question)

    return deduped_questions[: max(0, int(max_questions))]


def extract_ambiguity_questions(transcript: str, max_questions: int = 3) -> List[str]:
    return extract_ambiguity_questions_from_preprocessed_text(
        preprocessed_text=transcript,
        max_questions=max_questions,
    )


def _load_employee_dictionary_items() -> List[Tuple[str, str]]:
    try:
        from dictionary.employee_dictionary import load_employee_dictionary  # type: ignore

        return _normalize_dictionary_items(load_employee_dictionary())
    except Exception:
        return []


def _build_employee_dictionary_rule_text(employee_dictionary: List[Tuple[str, str]]) -> str:
    lines: List[str] = []
    lines.append("＜社員辞書＞")
    if employee_dictionary:
        for source, preferred in employee_dictionary:
            lines.append(f"- {source} → {preferred}")
    else:
        lines.append("- なし")
    return "\n".join(lines)


def detect_ambiguity_questions(cleaned_transcript: str, file_client_name: str = "") -> List[str]:
    text = str(cleaned_transcript or "").strip()
    if not text:
        return []
    client_name_from_filename = str(file_client_name or "").strip()

    company_dictionary = _normalize_dictionary_items(load_company_dictionary())
    abbreviation_dictionary = _normalize_dictionary_items(load_abbreviation_dictionary())
    employee_dictionary = _load_employee_dictionary_items()
    dictionary_rule_text = _build_dictionary_rule_text(
        company_dictionary=company_dictionary,
        abbreviation_dictionary=abbreviation_dictionary,
    )
    employee_dictionary_rule_text = _build_employee_dictionary_rule_text(employee_dictionary)

    model = getattr(settings, "GPT_PREPROCESS_MODEL", None) or getattr(
        settings, "OPENAI_GPT_PREPROCESS_MODEL", "gpt-4.1-mini"
    )
    timeout = float(getattr(settings, "GPT_PREPROCESS_TIMEOUT", 60))

    prompt = f"""以下はGPT前処理後の会議テキストです。
不明点を検出し、確認すべき質問のみを最大3件で出力してください。

[ファイル名から取得した顧客名]
{client_name_from_filename or "(なし)"}

[固有名詞判定に使う辞書]
{dictionary_rule_text}
{employee_dictionary_rule_text}

条件:
- 出力は質問文のみ（最大3件）
- 優先順位は必ず「決定事項 → Next Action → 顧客名 → 固有名詞」の順にする
- 決定事項: 存在しない、または内容が曖昧なら質問対象
- Next Action: 担当者 / 期限 / 内容 が曖昧なら質問対象
- 顧客名: 会議の主対象会社がファイル名顧客名とズレる場合のみ質問対象
- 固有名詞: 会社辞書・略称辞書・社員辞書に無い語を候補として質問対象
- 質問文の形式は次に厳密一致させる
    - 決定事項: この会議で決まったことは何ですか？
    - Next Action(担当者): この会議のNext Actionの担当者は何ですか？
    - Next Action(期限): この会議のNext Actionの期限は何ですか？
    - Next Action(内容): この会議のNext Actionの内容は何ですか？
    - 顧客名: この会議の顧客名は何ですか？
    - 固有名詞: ◯◯とは何ですか？（◯◯は怪しい語を1語のみ）
- 会議の主対象会社は、テキスト全体の文脈からGPTが判断する
- 顧客名不一致の判定は「会議全体の主対象会社」がファイル名顧客名とズレる場合のみ対象にする
- 比較例・導入事例・他社言及は顧客名不一致として扱わない
- 不明点がなければ「なし」とだけ出力

[入力]
{text}
"""

    client = OpenAI(api_key=settings.OPENAI_API_KEY)

    try:
        response = client.responses.create(
            model=model,
            temperature=0,
            input=prompt,
            timeout=timeout,
        )
        raw = _extract_text_from_response(response)
    except Exception as exc:
        logger.warning("detect_ambiguity_questions failed: %s", exc)
        return []

    if not raw:
        return []

    raw_text = raw.strip()
    if raw_text == "なし":
        return []

    questions: List[str] = []
    decision_questions: List[str] = []
    next_action_owner_questions: List[str] = []
    next_action_deadline_questions: List[str] = []
    next_action_content_questions: List[str] = []
    customer_questions: List[str] = []
    proper_noun_questions: List[str] = []

    def _normalize_question_text(question: str, category: str) -> str:
        q = question.strip()
        if q.startswith("- "):
            q = q[2:].strip()
        q = q.rstrip("。")

        if category == "proper":
            source = re.sub(r"とは何ですか？?$", "", q).strip()
            source = re.sub(r"は何ですか？?$", "", source).strip()
            source = re.sub(r"ですか？?$", "", source).strip()

            quoted = re.findall(r"[「『](.+?)[」』]", source)
            if quoted:
                term = quoted[0].strip()
            else:
                token_candidates = re.findall(r"[A-Za-z0-9_\-\u30A1-\u30FA\u4E00-\u9FFF]{2,}", source)
                term = token_candidates[0].strip() if token_candidates else ""

            return f"{term}とは何ですか？" if term else "固有名詞とは何ですか？"

        q = re.sub(r"とは何ですか？?$", "", q).strip()
        q = re.sub(r"は何ですか？?$", "", q).strip()
        q = re.sub(r"ですか？?$", "", q).strip()
        return f"{q}は何ですか？" if q else "確認事項は何ですか？"

    for line in raw_text.splitlines():
        q = _strip_number_prefix(line).strip()
        if not q:
            continue
        lower_q = q.lower()
        if (
            "決定" in q
            or "決まった" in q
            or "合意" in q
            or "決定事項" in q
        ):
            q = "この会議で決まったことは何ですか？"
            if len(decision_questions) < 3:
                decision_questions.append(q)
        elif (
            "next action" in lower_q
            or "担当" in q
            or "期限" in q
            or "いつまで" in q
            or "何をする" in q
        ):
            if any(keyword in q for keyword in ["担当", "誰", "だれ"]):
                q = "この会議のNext Actionの担当者は何ですか？"
                if q not in next_action_owner_questions:
                    next_action_owner_questions.append(q)
            elif any(keyword in q for keyword in ["期限", "いつまで", "日付", "締切"]):
                q = "この会議のNext Actionの期限は何ですか？"
                if q not in next_action_deadline_questions:
                    next_action_deadline_questions.append(q)
            else:
                q = "この会議のNext Actionの内容は何ですか？"
                if q not in next_action_content_questions:
                    next_action_content_questions.append(q)
        elif (
            "顧客" in q
            or "顧客名" in q
            or "ファイル名" in q
            or "クライアント" in q
            or "一致" in q
        ):
            if "一致" in q or "ズレ" in q or "不一致" in q:
                q = "この会議の顧客名は何ですか？"
            else:
                q = _normalize_question_text(q, "customer")
            if q not in customer_questions:
                customer_questions.append(q)
        else:
            q = _normalize_question_text(q, "proper")
            if q not in proper_noun_questions:
                proper_noun_questions.append(q)

    priority_groups = [
        decision_questions,
        next_action_owner_questions,
        next_action_deadline_questions,
        next_action_content_questions,
        customer_questions,
        proper_noun_questions,
    ]

    for group in priority_groups:
        for question in group:
            questions.append(question)
            if len(questions) >= 3:
                return questions

    return questions


_GENERIC_QUESTION_PATTERNS = [
    "他に何かありますか",
    "詳細を教えてください",
    "補足お願いします",
    "他にありますか",
    "教えてください",
]


def _normalize_single_line_question(raw_text: str) -> str:
    text = str(raw_text or "").strip()
    if not text:
        return ""

    text = text.replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.split(r"(?<=[。！？?!])\s+", text)[0].strip()
    text = text.rstrip("。 ")
    if not text:
        return ""

    if "?" in text and "？" not in text:
        text = text.replace("?", "？")
    if "？" not in text:
        text = f"{text}？"

    if any(pattern in text for pattern in _GENERIC_QUESTION_PATTERNS):
        return ""

    return text


def detect_bottleneck_question(
    cleaned_transcript: str,
    minutes_markdown: str,
    previous_answers: Optional[List[Dict[str, Any]]] = None,
    max_question_count: int = 3,
) -> str:
    transcript_text = str(cleaned_transcript or "").strip()
    minutes_text = str(minutes_markdown or "").strip()
    if not transcript_text:
        return ""

    answers = previous_answers or []
    answered_count = len([item for item in answers if isinstance(item, dict)])
    if answered_count >= max(0, int(max_question_count)):
        return ""

    answers_text_lines: List[str] = []
    for item in answers:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        answer = str(item.get("answer") or "").strip()
        if not question:
            continue
        answers_text_lines.append(f"- Q: {question} / A: {answer or '(スキップ)'}")
    answers_text = "\n".join(answers_text_lines) if answers_text_lines else "- なし"

    model = getattr(settings, "GPT_PREPROCESS_MODEL", None) or getattr(
        settings,
        "OPENAI_GPT_PREPROCESS_MODEL",
        "gpt-4.1-mini",
    )
    timeout = float(getattr(settings, "GPT_PREPROCESS_TIMEOUT", 60))

    prompt = f"""あなたは会議逐語録の意味成立を確認するアシスタントです。
以下のルールを厳守して、確認質問を最大1件だけ出力してください。

【目的】
- 逐語録の「意味が通らない箇所」を解消するための最小限の確認のみ
- 議事録を立派にするための補完は禁止

【質問してよい条件（以下のみ）】
1. 指示語・代名詞の参照先が不明
   例：「それ」「あれ」「この件」が何を指すか、逐語録を読んでも特定不能
2. 主語・発言主体が不明
   例：誰の判断・誰の担当の話か、逐語録から読み取れない
3. 固有名詞の同一性が不明
   例：同一人物・同一事項の表記が逐語録中で一致しない
4. 前後を読んでも文脈として意味が通らない箇所
   例：逐語録のみでは何の話題か特定不能

【質問してはいけない条件（完全禁止）】
- Next Actionの確認・担当者の確認
- 決定事項の内容確認
- 会議の結論・方向性の確認
- 議事録の構造を整えるための質問
- 逐語録に出ていない情報の補完・確認
- 「他に何かありますか？」系の問い

【変更履歴（重複回避）】
{answers_text}

【出力形式】
- 質問文を1文のみ出力（改行・説明・JSON不要）
- 質問の根拠は必ず逐語録内に存在する記述であること
- 該当箇所がなければ「なし」とのみ出力

【逐語録】
{transcript_text}

【現在の議事録Markdown】
{minutes_text}
"""

    try:
        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        response = client.responses.create(
            model=model,
            temperature=0,
            input=prompt,
            timeout=timeout,
        )
        raw = _extract_text_from_response(response)
    except Exception as exc:
        logger.warning("detect_bottleneck_question failed: %s", exc)
        return ""

    if not raw:
        return ""

    candidate = _normalize_single_line_question(raw)
    if candidate in {"", "なし", "なし？"}:
        return ""
    return candidate
