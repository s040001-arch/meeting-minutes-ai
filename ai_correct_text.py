import argparse
import json
import os
import platform
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
    url = "https://api.openai.com/v1/responses"
    payload = {
        "model": model,
        "max_output_tokens": 12000,
        "input": [
            {
                "role": "system",
                "content": (
                    "あなたは議事録の文字起こし整形アシスタントです。"
                    "入力テキストの全文を保持したまま、誤変換の修正と文の整形のみを行ってください。"
                    "要約・省略・言い換えによる情報圧縮は禁止です。"
                    "文・段落・箇条書き・数値・固有名詞を削除しないでください。"
                    "不明な箇所を推測で補完しないでください。"
                    "入力が長文でも省略・要約せず、全内容を保持したまま出力してください。途中で打ち切らないでください。"
                    "許可される処理は次の範囲のみです: "
                    "1) 明らかな誤字脱字/誤変換の修正 "
                    "2) 句読点・改行・文区切りの調整 "
                    "3) フィラーの最小限の除去。"
                    "削除してよいのは、意味を変えない軽微なノイズに限定します。"
                    "重複する挨拶（例：お疲れ様ですの連続）、意味を持たない相槌の連打（例：はい、はい、はい）、"
                    "明らかなフィラー（例：えーと、あのー、ま）、"
                    "同一内容の言い直しで、直後に明確に同一内容が繰り返されている場合のみ、前半の重複部分を削除してよいです。"
                    "出力は入力と同程度の情報量・長さを維持してください。"
                    "上記以外は原文を保持し、要約・圧縮はしないでください。"
                    "削除に迷う場合は削除せず、そのまま残してください。"
                    "出力は整形後テキスト本文のみとし、説明文や注釈は付けないでください。"
                ),
            },
            {"role": "user", "content": text},
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

    # 診断ログ（挙動確認用）: 既存ロジックは変えずに観測情報のみ追加
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

    # Responses API の最小抽出ロジック
    texts: list[str] = []
    for item in output_items:
        for c in item.get("content", []):
            if c.get("type") == "output_text":
                texts.append(c.get("text", ""))

    corrected = "\n".join(t for t in texts if t).strip()
    print(f"debug_openai_output_text_len={len(corrected)}")
    if not corrected:
        raise RuntimeError("OpenAI response did not contain output_text.")
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
        default="gpt-4o-mini",
        help="OpenAIモデル名（デフォルト: gpt-4o-mini）",
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

