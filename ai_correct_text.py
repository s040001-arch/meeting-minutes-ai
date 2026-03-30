import argparse
import json
import os
import platform
import re
import urllib.error
import urllib.request


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
        )
    return (
        base
        + "意味を変えない範囲で文構造の再構成を積極的に行ってください。"
        + "長すぎる文は分割し、話題ごとに段落分けしてください。"
        + "フィラー（例: うん、なんか、えーと）を削除し、言いよどみや重複表現を整理してください。"
        + "語彙の推測置換は禁止です（例: 「フード改革」を別語へ置換しない）。"
    )


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

    actual_placeholders = _extract_placeholders_strict(corrected_masked)
    placeholder_ok = (actual_placeholders == expected_placeholders)
    if not placeholder_ok:
        raise RuntimeError(
            "placeholder_sequence_mismatch "
            f"expected={expected_placeholders} actual={actual_placeholders}"
        )

    corrected = _restore_masked_text(corrected_masked, placeholder_mapping)
    stats_after = compute_readability_stats(corrected)
    meta: dict[str, object] = {
        "stats_before": stats_before,
        "stats_after": stats_after,
        "placeholder_ok": True,
        "aggressive_structure": aggressive_structure,
    }
    return corrected, meta


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

