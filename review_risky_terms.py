import argparse
import json
import os
import urllib.error
import urllib.request

from ai_correct_text import resolve_openai_api_key


def resolve_input_path(job_id: str, input_path: str | None, input_root: str) -> str:
    if input_path:
        return input_path
    return os.path.join(input_root, job_id, "merged_transcript_ai.txt")


def split_text(text: str, chunk_size: int) -> list[str]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be > 0")
    chunks: list[str] = []
    i = 0
    while i < len(text):
        chunks.append(text[i : i + chunk_size])
        i += chunk_size
    return chunks


def _extract_json_array(text: str) -> list[dict]:
    text = text.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [x for x in parsed if isinstance(x, dict)]
    except json.JSONDecodeError:
        pass

    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    candidate = text[start : end + 1]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [x for x in parsed if isinstance(x, dict)]


def call_openai_risky_terms_review(
    text: str,
    model: str,
    api_key: str,
    timeout_sec: int = 180,
) -> list[dict]:
    url = "https://api.openai.com/v1/responses"
    payload = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": (
                    "あなたは議事録レビュー支援です。"
                    "本文を書き換えず、危険語候補のみ抽出してください。"
                    "抽出対象は、人名候補・組織名候補・商品名/サービス名候補・誤変換の可能性が高い語です。"
                    "可能なら数字や役割に関する怪しい表現も含めてください。"
                    "出力はJSON配列のみで返し、各要素は必ず type/text/reason を含めてください。"
                    "type は proper_noun_candidate / organization_candidate / service_candidate / suspicious_word / suspicious_number_or_role のいずれかを使ってください。"
                    "確信が低いものは無理に出さないでください。"
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
    output_items = result.get("output", [])
    texts: list[str] = []
    for item in output_items:
        for content_item in item.get("content", []):
            if content_item.get("type") == "output_text":
                texts.append(str(content_item.get("text") or ""))

    reviewed_text = "\n".join(t for t in texts if t).strip()
    if not reviewed_text:
        return []
    return _extract_json_array(reviewed_text)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="step_4_35: 補正本文は変更せず、危険語候補を抽出する"
    )
    parser.add_argument("--job-id", required=True, help="対象ジョブID")
    parser.add_argument(
        "--input",
        default=None,
        help="入力テキスト（未指定時: merged_transcript_ai.txt）",
    )
    parser.add_argument(
        "--input-root",
        default="data/transcriptions",
        help="ジョブディレクトリのルート（デフォルト: data/transcriptions）",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="出力先（未指定時: {input_root}/{job_id}/risky_terms.json）",
    )
    parser.add_argument("--model", default="gpt-4o", help="OpenAIモデル名（デフォルト: gpt-4o）")
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=4000,
        help="長文を分割して抽出するチャンク文字数（デフォルト: 4000）",
    )
    args = parser.parse_args()

    input_path = resolve_input_path(args.job_id, args.input, args.input_root)
    if not os.path.isfile(input_path):
        raise FileNotFoundError(f"input file not found: {input_path}")
    if args.chunk_size <= 0:
        raise ValueError("--chunk-size must be > 0")

    with open(input_path, "r", encoding="utf-8") as f:
        text = f.read()

    api_key, key_source = resolve_openai_api_key()
    print(f"debug_openai_api_key_found={bool(api_key)}")
    print(f"debug_openai_api_key_source={key_source}")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set.")

    chunks = split_text(text, args.chunk_size)
    all_items: list[dict] = []
    for i, chunk in enumerate(chunks, start=1):
        items = call_openai_risky_terms_review(
            text=chunk,
            model=args.model,
            api_key=api_key,
        )
        all_items.extend(items)
        print(f"chunk={i}/{len(chunks)} extracted={len(items)}")

    output_path = args.output or os.path.join(args.input_root, args.job_id, "risky_terms.json")
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_items, f, ensure_ascii=False, indent=2)

    print(f"job_id={args.job_id}")
    print(f"input={input_path}")
    print(f"output={output_path}")
    print(f"chunks={len(chunks)}")
    print(f"items={len(all_items)}")
    print(f"model={args.model}")


if __name__ == "__main__":
    main()
