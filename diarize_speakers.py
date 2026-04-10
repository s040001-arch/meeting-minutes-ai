"""
Step 4.4: 話者識別・ターン分割（Speaker Diarization）

AI補正済みテキストから話者を推定し、ターン分割された逐語録を生成する。
音声レベルのダイアライゼーションではなく、文脈・内容・立場から話者を推定する。
"""
import argparse
import os
import re
import sys
import time
from typing import Any

import anthropic
import httpx

from filename_hints import extract_filename_hints, format_hints_for_prompt
from job_context import format_context_for_prompt, load_job_context

DIARIZE_MODEL = "claude-sonnet-4-20250514"
_TIMEOUT_SEC = 600
_RETRY_BACKOFF_SEC = (5.0, 10.0)


def _build_diarize_system_prompt(
    filename_hints: list[str] | None = None,
    job_context: dict | None = None,
) -> str:
    return (
        "あなたは会議の逐語録を整形する専門アシスタントです。\n"
        "入力テキストは音声認識から生成された補正済みの会議テキストです。\n"
        "以下のルールに従って、話者を特定しターン分割された逐語録を出力してください。\n"
        "説明文・前置き・注釈は一切付けないでください。\n"
        "\n【話者特定ルール】\n"
        "1. テキスト内の文脈から話者を推定する\n"
        "   - 自己紹介・所属説明（「我々の部署が」「弊社では」等）\n"
        "   - 立場の違い（依頼者 vs 提案者、質問する側 vs 説明する側）\n"
        "   - 価格提示・サービス説明をする側とされる側\n"
        "   - テキスト内で名前が言及されている場合はその名前を使う\n"
        "2. 推定できた話者名を「名前：」の形式でラベル付けする\n"
        "3. 名前が推定できない場合は「話者A：」「話者B：」とする\n"
        "4. 話者は会議の最初から最後まで一貫したラベルを使う\n"
        "\n【ターン分割ルール】\n"
        "1. 話者が交代する箇所でターンを分割する\n"
        "2. 各ターンは「話者名：発言内容」の形式とする\n"
        "3. ターン間には空行を1行入れる\n"
        "4. 同一話者の連続発言は1つのターンにまとめる\n"
        "\n【相槌の扱い】\n"
        "1. 短い相槌（「うん」「はい」「ええ」「なるほど」「へえ」等）で"
        "相手の発言に挟まっている場合は、独立したターンとして分離する\n"
        "2. 自分の発言を始める際の「はい」「うん、」はその発言に含める\n"
        "3. 意味のない相槌の連続（「うん、うん、うん」等）は"
        "短く整理してよい（例：「うん。」に圧縮）\n"
        "\n【発言の途切れ・整形】\n"
        "1. 発言が途中で他の話者に遮られる箇所にはダッシュ「——」を挿入する\n"
        "2. 句読点が不足している箇所は自然な位置で補う\n"
        "3. 疑問文には適切に「？」を付ける\n"
        "\n【禁止事項】\n"
        "1. 内容の要約・追加・削除は禁止\n"
        "2. 話者の発言内容を書き換えてはならない\n"
        "3. 推測による固有名詞の変更は禁止\n"
        "4. 口語表現（「なんて言うんでしょう」「フルフル」等）はそのまま維持する\n"
        "5. 文法的に不完全な文も逐語録として維持する\n"
        "6. 社内用語・造語（「鬼メンタル」「ラポールメーカー」等）はそのまま維持する\n"
        + format_hints_for_prompt(filename_hints or [])
        + format_context_for_prompt(job_context or {})
    )


def diarize_transcript(
    text: str,
    *,
    filename_hints: list[str] | None = None,
    job_context: dict[str, Any] | None = None,
    model: str | None = None,
    timeout_sec: int = _TIMEOUT_SEC,
) -> str:
    """補正済みテキストに話者ラベルとターン分割を適用する。"""
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")

    use_model = model or os.getenv("DIARIZE_MODEL", "").strip() or DIARIZE_MODEL
    system_prompt = _build_diarize_system_prompt(
        filename_hints=filename_hints,
        job_context=job_context,
    )

    client = anthropic.Anthropic(
        api_key=api_key,
        timeout=httpx.Timeout(timeout=float(timeout_sec), connect=30.0),
    )

    max_attempts = len(_RETRY_BACKOFF_SEC) + 1
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        started_at = time.monotonic()
        print(
            f"diarize_speakers_started attempt={attempt}/{max_attempts} "
            f"input_chars={len(text)} model={use_model}",
            flush=True,
        )
        full_response = ""
        try:
            with client.messages.stream(
                model=use_model,
                max_tokens=max(len(text) * 3, 8000),
                system=system_prompt,
                messages=[{"role": "user", "content": text}],
            ) as stream:
                for chunk in stream.text_stream:
                    full_response += chunk

            elapsed = time.monotonic() - started_at
            print(
                f"diarize_speakers_completed attempt={attempt}/{max_attempts} "
                f"output_chars={len(full_response)} elapsed={elapsed:.1f}s",
                flush=True,
            )

            result = full_response.strip()
            if not result:
                raise ValueError("diarize returned empty output")

            if not _has_speaker_labels(result):
                print(
                    "diarize_speakers_warning: output has no speaker labels, "
                    "returning original text",
                    file=sys.stderr,
                    flush=True,
                )
                return text

            return result

        except Exception as e:
            last_error = e
            elapsed = time.monotonic() - started_at
            print(
                f"diarize_speakers_failed attempt={attempt}/{max_attempts} "
                f"elapsed={elapsed:.1f}s error={e!r}",
                flush=True,
            )
            if attempt >= max_attempts:
                break
            backoff = _RETRY_BACKOFF_SEC[attempt - 1]
            print(f"diarize_speakers_retrying backoff={backoff:.1f}s", flush=True)
            time.sleep(backoff)

    print(
        f"diarize_speakers_all_attempts_failed error={last_error!r} "
        "returning original text",
        file=sys.stderr,
        flush=True,
    )
    return text


def _has_speaker_labels(text: str) -> bool:
    """出力に話者ラベル（「名前：」形式）が含まれているか判定する。"""
    pattern = re.compile(r"^.{1,20}：", re.MULTILINE)
    matches = pattern.findall(text)
    return len(matches) >= 3


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Step 4.4: 話者識別・ターン分割"
    )
    parser.add_argument("--input", required=True, help="入力テキストファイル")
    parser.add_argument("--output", default=None, help="出力先（未指定時: 入力ファイルを上書き）")
    parser.add_argument("--job-id", default=None)
    parser.add_argument("--input-root", default="data/transcriptions")
    parser.add_argument("--model", default=None, help="使用モデル（未指定時: claude-sonnet-4）")
    args = parser.parse_args()

    if not os.path.isfile(args.input):
        raise FileNotFoundError(f"input not found: {args.input}")

    with open(args.input, "r", encoding="utf-8") as f:
        text = f.read()

    hints: list[str] = []
    job_context: dict[str, Any] | None = None
    if args.job_id:
        stem = args.job_id
        hints = extract_filename_hints(stem)
        ctx_path = os.path.join(args.input_root, args.job_id, "context.json")
        if os.path.isfile(ctx_path):
            job_context = load_job_context(ctx_path)

    result = diarize_transcript(
        text,
        filename_hints=hints,
        job_context=job_context,
        model=args.model,
    )

    out_path = args.output or args.input
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(result)

    print(f"input={args.input}")
    print(f"output={out_path}")
    print(f"input_chars={len(text)}")
    print(f"output_chars={len(result)}")
    print(f"has_labels={_has_speaker_labels(result)}")


if __name__ == "__main__":
    main()
