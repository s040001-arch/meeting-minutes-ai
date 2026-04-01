import argparse
import anthropic
import httpx
import json
import logging
import os
import platform
import re
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


def resolve_input_path(job_id: str, input_path: str | None, input_root: str) -> str:
    if input_path:
        return input_path
    return os.path.join(
        input_root,
        job_id,
        "merged_transcript_mechanical.txt",
    )


def _normalize_api_key(raw: str | None) -> str | None:
    if not raw:
        return None
    v = raw.strip()
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        v = v[1:-1].strip()
    return v or None


def _load_api_key_from_windows_user_env() -> str | None:
    if platform.system().lower() != "windows":
        return None
    try:
        import winreg  # type: ignore

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment")
        value, _ = winreg.QueryValueEx(key, "OPENAI_API_KEY")
        winreg.CloseKey(key)
        return _normalize_api_key(value)
    except Exception:
        return None


def resolve_openai_api_key() -> tuple[str | None, str]:
    # 1) 現在プロセス環境変数（通常ケース）
    key = _normalize_api_key(os.getenv("OPENAI_API_KEY"))
    if key:
        return key, "process_env"

    # 2) Windows setx直後のフォールバック（ユーザー環境変数レジストリ）
    key = _load_api_key_from_windows_user_env()
    if key:
        return key, "windows_user_env"

    return None, "not_found"


def split_mechanical_for_ai_correction(
    text: str,
    *,
    target_chars: int = 5000,
    lookback: int = 1500,
    hard_max: int = 12000,
) -> list[str]:
    """
    長文AI補正用に機械補正テキストをチャンクに分割する。
    目標文字数付近で切り、可能なら直近の改行または句点へ後方スナップする。
    単一ブロックが極端に長い場合は hard_max でハードカットする。
    連結すると元テキストと一致する（損失なし）。
    """
    if not text:
        return []
    n = len(text)
    chunks: list[str] = []
    start = 0
    while start < n:
        remaining = n - start
        if remaining <= target_chars:
            chunks.append(text[start:n])
            break
        t_end = min(start + target_chars, n)
        lo = max(start, t_end - lookback)
        split_at = -1
        for i in range(t_end - 1, lo - 1, -1):
            c = text[i]
            if c in "\n\r":
                split_at = i + 1
                break
            if c in "。．":
                split_at = i + 1
                break
        if split_at <= start:
            if t_end - start > hard_max:
                split_at = start + hard_max
            else:
                split_at = t_end
        if split_at <= start:
            split_at = min(start + hard_max, n)
        if split_at <= start:
            split_at = min(start + 1, n)
        chunk = text[start:split_at]
        chunks.append(chunk)
        start = split_at
    return chunks


_PLACEHOLDER_STRICT_RE = re.compile(r"<[A-Z]+_[0-9]{4}>")
_FILLER_RE = re.compile(r"(?:うん|なんか|えーと|えっと|あのー?|ま)(?:、|。|\\s|$)")
_SENTENCE_SPLIT_RE = re.compile(r"[。！？]")
_TOPIC_BREAK_HINT_RE = re.compile(
    r"(?:^|\n)\s*(?:で、|ただ、|一方で、|一方、|あと、|なので、|まず、|次に、|つまり、|そのうえで、|一方で|ちなみに、)"
)

# Priority (small -> large):
# MONEY / DATE / PERSON / COMPANY should win over generic NUM.
_MASK_PATTERN_SPECS: list[tuple[str, re.Pattern[str], int]] = [
    (
        "MONEY",
        re.compile(
            r"(?:¥\s*\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*(?:円|万円|億円|千円|百万円|兆円)"
        ),
        10,
    ),
    (
        "DATE",
        re.compile(
            r"(?:\d{4}[/-]\d{1,2}[/-]\d{1,2}|\d{4}年\d{1,2}月\d{1,2}日|\d{1,2}月\d{1,2}日|\d{1,2}:\d{2}|\d{1,2}時(?:\d{1,2}分)?)"
        ),
        20,
    ),
    (
        "PERSON",
        re.compile(r"[\u4E00-\u9FFF]{1,4}(?:さん|氏|様|殿)"),
        30,
    ),
    (
        "COMPANY",
        re.compile(
            r"(?:株式会社[\u4E00-\u9FFF\u3040-\u30FFA-Za-z0-9・&＆\-]{1,24}|[\u4E00-\u9FFF\u3040-\u30FFA-Za-z0-9・&＆\-]{1,24}(?:社|グループ|ホールディングス))"
        ),
        40,
    ),
    (
        "NUM",
        re.compile(r"\d{1,3}(?:,\d{3})*(?:\.\d+)?(?:\s*(?:社|人|名|問|回|件|本|台|社数|日|時間|分))?"),
        90,
    ),
]


def _extract_placeholders_strict(text: str) -> list[str]:
    return [m.group(0) for m in _PLACEHOLDER_STRICT_RE.finditer(text)]


def _mask_protected_tokens(text: str) -> tuple[str, dict[str, str], list[str]]:
    candidates: list[tuple[int, int, int, str]] = []
    for p_type, pattern, priority in _MASK_PATTERN_SPECS:
        for m in pattern.finditer(text):
            s, e = m.span()
            if s >= e:
                continue
            candidates.append((s, e, priority, p_type))

    # start asc, priority asc, length desc
    candidates.sort(key=lambda x: (x[0], x[2], -(x[1] - x[0])))

    selected: list[tuple[int, int, str]] = []
    last_end = -1
    for s, e, _priority, p_type in candidates:
        if s < last_end:
            continue
        selected.append((s, e, p_type))
        last_end = e

    counters: dict[str, int] = {}
    mapping: dict[str, str] = {}
    expected: list[str] = []

    out_parts: list[str] = []
    pos = 0
    for s, e, p_type in selected:
        if s < pos:
            continue
        original = text[s:e]
        if not original.strip():
            continue
        out_parts.append(text[pos:s])
        counters[p_type] = counters.get(p_type, 0) + 1
        placeholder = f"<{p_type}_{counters[p_type]:04d}>"
        out_parts.append(placeholder)
        mapping[placeholder] = original
        expected.append(placeholder)
        pos = e
    out_parts.append(text[pos:])
    return "".join(out_parts), mapping, expected


def _restore_masked_text(masked_text: str, mapping: dict[str, str]) -> str:
    def repl(m: re.Match[str]) -> str:
        token = m.group(0)
        return mapping.get(token, token)

    return _PLACEHOLDER_STRICT_RE.sub(repl, masked_text)


def _validate_placeholder_sequence(
    corrected_masked: str,
    expected_placeholders: list[str],
) -> None:
    actual_placeholders = _extract_placeholders_strict(corrected_masked)
    if actual_placeholders != expected_placeholders:
        raise RuntimeError(
            "placeholder_sequence_mismatch "
            f"expected={expected_placeholders} actual={actual_placeholders}"
        )


def _count_paragraphs(text: str) -> int:
    blocks = [b for b in re.split(r"\n\s*\n+", text) if b.strip()]
    return len(blocks)


def compute_readability_stats(text: str) -> dict[str, int]:
    return {
        "period_count": text.count("。"),
        "comma_count": text.count("、"),
        "newline_count": text.count("\n"),
        "paragraph_count": _count_paragraphs(text),
        "filler_count": len(_FILLER_RE.findall(text)),
    }


def compute_structure_quality(text: str) -> dict[str, float]:
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    sentence_count = len(sentences)
    paragraph_count = _count_paragraphs(text)
    sentence_lengths = [len(s) for s in sentences]
    avg_sentence_len = (sum(sentence_lengths) / sentence_count) if sentence_count else 0.0
    short_sentence_rate = (
        (sum(1 for n in sentence_lengths if n <= 8) / sentence_count) if sentence_count else 0.0
    )
    long_sentence_rate = (
        (sum(1 for n in sentence_lengths if n >= 80) / sentence_count) if sentence_count else 0.0
    )
    sentences_per_paragraph = (sentence_count / paragraph_count) if paragraph_count > 0 else 0.0
    topic_break_hint_count = len(_TOPIC_BREAK_HINT_RE.findall(text))

    # 5-item rubric: prioritize "readable structure", not raw punctuation counts.
    rubric = {
        "has_enough_sentences": 1.0 if sentence_count >= 2 else 0.0,
        "avg_sentence_len_band": 1.0 if 18.0 <= avg_sentence_len <= 65.0 else 0.0,
        "short_sentence_rate_ok": 1.0 if short_sentence_rate <= 0.40 else 0.0,
        "long_sentence_rate_ok": 1.0 if long_sentence_rate <= 0.25 else 0.0,
        "paragraph_density_ok": 1.0 if 1.2 <= sentences_per_paragraph <= 6.5 else 0.0,
    }
    quality_score = float(sum(rubric.values()))
    return {
        "sentence_count": float(sentence_count),
        "paragraph_count": float(paragraph_count),
        "avg_sentence_len": float(avg_sentence_len),
        "short_sentence_rate": float(short_sentence_rate),
        "long_sentence_rate": float(long_sentence_rate),
        "sentences_per_paragraph": float(sentences_per_paragraph),
        "topic_break_hint_count": float(topic_break_hint_count),
        "quality_score": quality_score,
        **rubric,
    }


def _build_system_prompt(*, aggressive_structure: bool) -> str:
    base = (
        "あなたは議事録の可読性整形アシスタントです。"
        "要約・言い換え・内容の追加/削除は禁止です。"
        "固有名詞・人名・会社名・日付・時刻・数値・金額・社数・スケジュールの推測補正は禁止です。"
        "不明語は意味推測で置換せず、そのまま残してください。"
        "入力には保護トークン <TYPE_0001> 形式が含まれます。"
        "この保護トークンは1文字も変更・削除・追加・並べ替えしてはいけません。"
        "保護トークンの出現順は入力と完全に一致させてください。"
        "出力は整形後テキスト本文のみとし、説明文や注釈は付けないでください。"
    )
    if not aggressive_structure:
        return (
            base
            + "意味を変えない範囲で、句読点・改行・段落・文境界・明らかな脱字/崩れの修正を行ってください。"
            + "軽微なフィラー削除と言いよどみ整理は許可します。"
            + "段落は文数よりも話題のまとまりを優先して調整してください。"
        )
    return (
        base
        + "意味を変えない範囲で文構造の再構成を積極的に行ってください。"
        + "長すぎる文は分割し、話題単位で段落分けしてください。"
        + "段落の文数目安は3-5文ですが、意味のまとまりを優先してください。"
        + "過分割（短文だけの段落乱立）は避け、理解しやすいまとまりにしてください。"
        + "フィラー（例: うん、なんか、えーと）を削除し、言いよどみや重複表現を整理してください。"
        + "語彙の推測置換は禁止です（例: 「フード改革」を別語へ置換しない）。"
    )


def _build_detection_system_prompt() -> str:
    return (
        "あなたは議事録補正の分析アシスタントです。"
        "入力はマスク済みの日本語テキストです。"
        "文脈が通らない箇所、明らかな脱字・崩れ・誤変換・接続不良だけを検出してください。"
        "各箇所について、修正にどれだけ推測が必要かを guess_level で 0 から 100 の整数で評価してください。"
        "0 はほぼ確実、100 はほぼ推測です。"
        "固有名詞・人名・会社名・日付・時刻・数値・金額・社数・スケジュールの推測補正は禁止です。"
        "不明語は意味推測で置換しないでください。"
        "保護トークン <TYPE_0001> 形式は1文字も変更・削除・追加・並べ替えしてはいけません。"
        "出力は JSON 配列のみとし、説明文・前置き・コードフェンスを付けないでください。"
        "各要素は location, original, issue, suggestion, guess_level の5キーを持つオブジェクトにしてください。"
        "location は該当箇所の前後10文字程度の短い抜粋、original は元の問題箇所そのもの、"
        "suggestion は修正候補、issue は何が問題か、guess_level は整数です。"
    )


def _stream_anthropic_text(
    *,
    api_key: str,
    model: str,
    system_prompt: str,
    user_message: str,
    max_tokens: int,
    timeout_sec: int,
    log_label: str,
) -> tuple[str, object | None]:
    client = anthropic.Anthropic(
        api_key=api_key,
        timeout=httpx.Timeout(timeout=float(timeout_sec), connect=30.0),
    )
    started_at = time.monotonic()
    logger.info(f"{log_label} started: input_len={len(user_message)}")
    full_response = ""
    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        for text_chunk in stream.text_stream:
            full_response += text_chunk
        final_message = stream.get_final_message()
    elapsed = time.monotonic() - started_at
    logger.info(
        f"{log_label} completed: output_len={len(full_response)} elapsed={elapsed:.1f}s"
    )
    return full_response, getattr(final_message, "stop_reason", None)


def _parse_detection_response(raw_text: str) -> list[dict[str, object]]:
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    data = json.loads(text)
    if not isinstance(data, list):
        raise RuntimeError("detection_response_not_list")
    parsed: list[dict[str, object]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        original = str(item.get("original", "")).strip()
        suggestion = str(item.get("suggestion", "")).strip()
        issue = str(item.get("issue", "")).strip()
        location = str(item.get("location", "")).strip()
        guess_raw = item.get("guess_level", 100)
        try:
            guess_level = int(guess_raw)
        except (TypeError, ValueError):
            guess_level = 100
        guess_level = max(0, min(100, guess_level))
        if not original:
            continue
        parsed.append(
            {
                "location": location,
                "original": original,
                "issue": issue,
                "suggestion": suggestion,
                "guess_level": guess_level,
            }
        )
    return parsed


def _apply_low_guess_replacements(
    masked_text: str,
    detections: list[dict[str, object]],
) -> tuple[str, int, int]:
    updated = masked_text
    auto_applied = 0
    skipped = 0
    for item in detections:
        original = str(item.get("original", ""))
        suggestion = str(item.get("suggestion", ""))
        guess_level = int(item.get("guess_level", 100))
        if guess_level >= 10:
            skipped += 1
            continue
        if not original or not suggestion or original == suggestion:
            skipped += 1
            continue
        pos = updated.find(original)
        if pos < 0:
            skipped += 1
            continue
        updated = updated[:pos] + suggestion + updated[pos + len(original):]
        auto_applied += 1
    return updated, auto_applied, skipped


def _correct_full_text_legacy(
    *,
    text: str,
    api_key: str,
    model: str,
    timeout_sec: int,
    max_tokens: int,
) -> str:
    masked_text, placeholder_mapping, expected_placeholders = _mask_protected_tokens(text)
    system_prompt = _build_system_prompt(aggressive_structure=False)
    full_response, stop_reason = _stream_anthropic_text(
        api_key=api_key,
        model=model,
        system_prompt=system_prompt,
        user_message=masked_text,
        max_tokens=max_tokens,
        timeout_sec=timeout_sec,
        log_label="AI correction streaming",
    )
    _LAST_CORRECT_FULL_TEXT_META["stop_reason"] = stop_reason
    if stop_reason == "max_tokens":
        _LAST_CORRECT_FULL_TEXT_META["used_fallback"] = True
        _LAST_CORRECT_FULL_TEXT_META["fallback_reason"] = "anthropic_max_tokens_reached"
        print(
            f"[WARNING] correct_full_text: fallback to original. "
            f"reason=anthropic_max_tokens_reached input_chars={len(text)}"
        )
        return text

    corrected_masked = full_response.strip()
    _LAST_CORRECT_FULL_TEXT_META["output_chars"] = len(corrected_masked)
    print(
        f"correct_full_text: output_chars={len(corrected_masked)} "
        f"stop_reason={stop_reason}"
    )
    if not corrected_masked:
        _LAST_CORRECT_FULL_TEXT_META["used_fallback"] = True
        _LAST_CORRECT_FULL_TEXT_META["fallback_reason"] = "anthropic_text_missing"
        print(
            f"[WARNING] correct_full_text: fallback to original. "
            f"reason=anthropic_text_missing input_chars={len(text)}"
        )
        return text

    _validate_placeholder_sequence(corrected_masked, expected_placeholders)
    restored = _restore_masked_text(corrected_masked, placeholder_mapping)
    _LAST_CORRECT_FULL_TEXT_META["output_chars"] = len(restored)
    print(f"correct_full_text: final_chars={len(restored)} (input was {len(text)})")
    return restored


def call_openai_for_correction_detailed(
    text: str,
    model: str,
    api_key: str,
    timeout_sec: int = 300,
    *,
    aggressive_structure: bool = False,
) -> tuple[str, dict[str, object]]:
    """
    OpenAI Responses API で議事録可読性整形を実行し、評価用メタを返す。
    """
    stats_before = compute_readability_stats(text)
    structure_before = compute_structure_quality(text)
    masked_text, placeholder_mapping, expected_placeholders = _mask_protected_tokens(text)

    url = "https://api.openai.com/v1/responses"
    payload = {
        "model": model,
        "temperature": 0.2,
        "max_output_tokens": 12000,
        "input": [
            {
                "role": "system",
                "content": _build_system_prompt(aggressive_structure=aggressive_structure),
            },
            {"role": "user", "content": masked_text},
        ],
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI API error: {e.code} {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"OpenAI API connection error: {e}") from e

    result = json.loads(body)
    response_status = result.get("status")
    incomplete_details = result.get("incomplete_details")
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    output_items = result.get("output", [])
    output_count = len(output_items) if isinstance(output_items, list) else 0
    print(f"debug_openai_response_status={response_status}")
    print(
        "debug_openai_response_incomplete_details="
        + json.dumps(incomplete_details, ensure_ascii=False)
    )
    print(
        "debug_openai_response_usage="
        + json.dumps(
            {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
            ensure_ascii=False,
        )
    )
    print(f"debug_openai_response_output_count={output_count}")

    texts: list[str] = []
    for item in output_items:
        for c in item.get("content", []):
            if c.get("type") == "output_text":
                texts.append(c.get("text", ""))

    corrected_masked = "\n".join(t for t in texts if t).strip()
    print(f"debug_openai_output_text_len={len(corrected_masked)}")
    if not corrected_masked:
        raise RuntimeError("OpenAI response did not contain output_text.")

    _validate_placeholder_sequence(corrected_masked, expected_placeholders)

    corrected = _restore_masked_text(corrected_masked, placeholder_mapping)
    stats_after = compute_readability_stats(corrected)
    structure_after = compute_structure_quality(corrected)
    meta: dict[str, object] = {
        "stats_before": stats_before,
        "stats_after": stats_after,
        "structure_before": structure_before,
        "structure_after": structure_after,
        "placeholder_ok": True,
        "aggressive_structure": aggressive_structure,
    }
    return corrected, meta


_LAST_CORRECT_FULL_TEXT_META: dict[str, object] = {}


def get_last_correct_full_text_meta() -> dict[str, object]:
    return dict(_LAST_CORRECT_FULL_TEXT_META)


def correct_full_text(
    text: str,
    model: str = "claude-sonnet-4-20250514",
    timeout_sec: int = 900,
) -> str:
    """
    全文を一括でAI補正する。チャンク分割なし。
    Anthropic Messages API を使用。
    戻り値は補正後テキスト。失敗時は元テキストを返す。
    """
    if not text:
        return text

    print(f"correct_full_text: input_chars={len(text)}")
    global _LAST_CORRECT_FULL_TEXT_META
    max_tokens = min(max(int(len(text) * 1.0), 8192), 40960)
    _LAST_CORRECT_FULL_TEXT_META = {
        "input_chars": len(text),
        "output_chars": 0,
        "stop_reason": None,
        "fallback_reason": None,
        "used_fallback": False,
        "max_tokens": max_tokens,
    }
    print(f"correct_full_text: request_max_tokens={max_tokens}")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")

    try:
        masked_text, placeholder_mapping, expected_placeholders = _mask_protected_tokens(text)
        detection_response, stop_reason = _stream_anthropic_text(
            api_key=api_key,
            model=model,
            system_prompt=_build_detection_system_prompt(),
            user_message=masked_text,
            max_tokens=max_tokens,
            timeout_sec=timeout_sec,
            log_label="AI correction detection streaming",
        )
        _LAST_CORRECT_FULL_TEXT_META["stop_reason"] = stop_reason
        if stop_reason == "max_tokens":
            print(
                f"[WARNING] correct_full_text: detection hit max_tokens. "
                "falling back to legacy full-text correction."
            )
            return _correct_full_text_legacy(
                text=text,
                api_key=api_key,
                model=model,
                timeout_sec=timeout_sec,
                max_tokens=max_tokens,
            )

        detections = _parse_detection_response(detection_response)
        detection_count = len(detections)
        auto_candidates = sum(1 for item in detections if int(item["guess_level"]) < 10)
        skipped_candidates = detection_count - auto_candidates
        print(
            f"correct_full_text: detection_count={detection_count} "
            f"auto_candidates={auto_candidates} skipped_candidates={skipped_candidates}"
        )

        corrected_masked, auto_applied, skipped_total = _apply_low_guess_replacements(
            masked_text,
            detections,
        )
        print(
            f"correct_full_text: auto_applied={auto_applied} "
            f"skipped_for_phase_b={skipped_total}"
        )
        _LAST_CORRECT_FULL_TEXT_META["output_chars"] = len(corrected_masked)

        _validate_placeholder_sequence(corrected_masked, expected_placeholders)
        restored = _restore_masked_text(corrected_masked, placeholder_mapping)
        _LAST_CORRECT_FULL_TEXT_META["output_chars"] = len(restored)
        print(f"correct_full_text: final_chars={len(restored)} (input was {len(text)})")
        return restored
    except json.JSONDecodeError as e:
        print(
            f"[WARNING] correct_full_text: detection JSON parse failed. "
            f"falling back to legacy full-text correction. error={e!r}"
        )
        try:
            return _correct_full_text_legacy(
                text=text,
                api_key=api_key,
                model=model,
                timeout_sec=timeout_sec,
                max_tokens=max_tokens,
            )
        except Exception as legacy_e:
            _LAST_CORRECT_FULL_TEXT_META["used_fallback"] = True
            _LAST_CORRECT_FULL_TEXT_META["fallback_reason"] = f"legacy_exception:{legacy_e!r}"
            print(
                f"[WARNING] correct_full_text: fallback to original. "
                f"reason=legacy_exception:{legacy_e!r} input_chars={len(text)}"
            )
            return text
    except RuntimeError as e:
        if str(e) == "detection_response_not_list":
            print(
                "[WARNING] correct_full_text: detection response was not JSON array. "
                "falling back to legacy full-text correction."
            )
            try:
                return _correct_full_text_legacy(
                    text=text,
                    api_key=api_key,
                    model=model,
                    timeout_sec=timeout_sec,
                    max_tokens=max_tokens,
                )
            except Exception as legacy_e:
                _LAST_CORRECT_FULL_TEXT_META["used_fallback"] = True
                _LAST_CORRECT_FULL_TEXT_META["fallback_reason"] = f"legacy_exception:{legacy_e!r}"
                print(
                    f"[WARNING] correct_full_text: fallback to original. "
                    f"reason=legacy_exception:{legacy_e!r} input_chars={len(text)}"
                )
                return text
        raise
    except httpx.TimeoutException as e:
        _LAST_CORRECT_FULL_TEXT_META["used_fallback"] = True
        _LAST_CORRECT_FULL_TEXT_META["fallback_reason"] = f"timeout:{e!r}:timeout_sec={timeout_sec}"
        print(
            f"[WARNING] correct_full_text: fallback to original. "
            f"reason=timeout:{e!r} timeout_sec={timeout_sec} input_chars={len(text)}"
        )
        return text
    except httpx.HTTPError as e:
        _LAST_CORRECT_FULL_TEXT_META["used_fallback"] = True
        _LAST_CORRECT_FULL_TEXT_META["fallback_reason"] = f"http_error:{e!r}"
        print(
            f"[WARNING] correct_full_text: fallback to original. "
            f"reason=http_error:{e!r} input_chars={len(text)}"
        )
        return text
    except Exception as e:
        _LAST_CORRECT_FULL_TEXT_META["used_fallback"] = True
        _LAST_CORRECT_FULL_TEXT_META["fallback_reason"] = f"exception:{e!r}"
        print(
            f"[WARNING] correct_full_text: fallback to original. "
            f"reason=exception:{e!r} input_chars={len(text)}"
        )
        return text


def call_openai_for_correction(
    text: str,
    model: str,
    api_key: str,
    timeout_sec: int = 300,
) -> str:
    """
    最小実装:
    OpenAI Responses API を直接呼び出し、自然な日本語へ整形する。
    """
    corrected, _meta = call_openai_for_correction_detailed(
        text=text,
        model=model,
        api_key=api_key,
        timeout_sec=timeout_sec,
        aggressive_structure=False,
    )
    return corrected


def call_openai_incorporate_answer(
    text: str,
    question_text: str,
    answer_text: str,
    model: str,
    api_key: str,
    timeout_sec: int = 120,
    *,
    excerpt_mode: bool = False,
) -> str:
    """
    Task 5-4: ユーザー回答を補正済み全文へ反映したうえで、再度整形する。
    excerpt_mode が True のときは text を発言録の一部として扱い、同範囲の更新後テキストのみを返す。
    """
    url = "https://api.openai.com/v1/responses"
    user_block = (
        "### 発言録\n"
        f"{text}\n\n"
        "### 確認していた質問\n"
        f"{question_text}\n\n"
        "### ユーザーの回答\n"
        f"{answer_text}"
    )
    if excerpt_mode:
        system_content = (
            "あなたは議事録整形アシスタントです。"
            "与えられた発言録は長文の一部抜粋です。確認質問へのユーザーの回答内容を反映して、この抜粋範囲のみを更新してください。"
            "回答が指す固有名詞・数値・主語などを正しく差し替え・補完し、それ以外の事実や文意は変えないでください。"
            "抜粋の前後に続く文脈と矛盾しないよう、この部分の表現だけを整えてください。"
            "不要な説明は出力せず、更新後の抜粋テキストのみを返してください（発言録の続きや前置きは付けないでください）。"
        )
    else:
        system_content = (
            "あなたは議事録整形アシスタントです。"
            "与えられた発言録全文に対し、確認質問へのユーザーの回答内容を反映して更新してください。"
            "回答が指す固有名詞・数値・主語などを正しく差し替え・補完し、それ以外の事実や文意は変えないでください。"
            "不要な説明は出力せず、更新後の発言録全文のみ返してください。"
        )
    payload = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": system_content,
            },
            {"role": "user", "content": user_block},
        ],
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI API error: {e.code} {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"OpenAI API connection error: {e}") from e

    result = json.loads(body)
    output_items = result.get("output", [])
    texts: list[str] = []
    for item in output_items:
        for c in item.get("content", []):
            if c.get("type") == "output_text":
                texts.append(c.get("text", ""))

    updated = "\n".join(t for t in texts if t).strip()
    if not updated:
        raise RuntimeError("OpenAI response did not contain output_text.")
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI補正（Task 4-3）: merged_transcript_mechanical.txt を自然文に整形"
    )
    parser.add_argument("--job-id", required=True, help="対象ジョブID")
    parser.add_argument(
        "--input",
        default=None,
        help="入力テキスト（未指定時は data/transcriptions/{job_id}/merged_transcript_mechanical.txt）",
    )
    parser.add_argument(
        "--input-root",
        default="data/transcriptions",
        help="入力ルートディレクトリ（デフォルト: data/transcriptions）",
    )
    parser.add_argument(
        "--model",
        default="gpt-4.1",
        help="OpenAIモデル名（デフォルト: gpt-4.1）",
    )
    args = parser.parse_args()

    input_path = resolve_input_path(args.job_id, args.input, args.input_root)
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"input file not found: {input_path}")

    api_key, key_source = resolve_openai_api_key()
    print(f"debug_openai_api_key_found={bool(api_key)}")
    print(f"debug_openai_api_key_source={key_source}")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. "
            "PowerShellを再起動するか、$env:OPENAI_API_KEY を現在セッションに設定してください。"
        )

    with open(input_path, "r", encoding="utf-8") as f:
        original_text = f.read()

    corrected_text = call_openai_for_correction(
        text=original_text,
        model=args.model,
        api_key=api_key,
    )

    print(f"job_id={args.job_id}")
    print(f"input={input_path}")
    print(f"model={args.model}")
    print("corrected_text=")
    print(corrected_text)


if __name__ == "__main__":
    main()

