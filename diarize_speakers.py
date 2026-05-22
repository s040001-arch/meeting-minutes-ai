"""
Step 4.4: 話者ターン分割（Speaker Turn Segmentation）

AI補正済みテキストから「話者が交代した箇所」を推定し、空行で区切る。
話者名の特定・ラベル付けは行わない（精度が低いため）。
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

# AI がラベルを付けてしまった場合に除去するパターン
_SPEAKER_LABEL_LINE_RE = re.compile(
    r"^(?:話者[A-ZＡ-Ｚ\d]+|[一-龠々ぁ-んァ-ヴー・A-Za-z０-９]{1,12}(?:さん|氏|様)?)[：:]\s*"
)


def _build_diarize_system_prompt(
    filename_hints: list[str] | None = None,
    job_context: dict | None = None,
) -> str:
    return (
        "あなたは会議の逐語録を整形する専門アシスタントです。\n"
        "入力テキストは音声認識から生成された補正済みの会議テキストです。\n"
        "以下のルールに従って、話者が交代した箇所で段落を分割してください。\n"
        "説明文・前置き・注釈は一切付けないでください。\n"
        "\n【最重要ルール：話者名ラベルは付けない】\n"
        "1. 「相原：」「話者A：」のような話者名ラベルは絶対に付けない\n"
        "2. 発言内容だけを出力する\n"
        "3. 話者が交代したと判断した箇所では、段落の間に空行を1行入れる\n"
        "4. 同一話者の連続発言は1段落にまとめる\n"
        "\n【話者交代の判断材料】\n"
        "1. 質問と回答の切り替わり\n"
        "2. 自己紹介・所属説明の前後\n"
        "3. 依頼者と提案者、説明する側と聞く側の切り替わり\n"
        "4. 相槌だけの短い発言（「うん」「はい」「なるほど」等）が独立した応答として現れた場合\n"
        "\n【相槌の扱い】\n"
        "1. 相手の発言に挟まれた短い相槌は、独立した段落として分離してよい\n"
        "2. 自分の発言を始める際の「はい、」「うん、」はその段落に含める\n"
        "3. 意味のない相槌の連続（「うん、うん、うん」等）は「うん。」に圧縮してよい\n"
        "\n【発言の途切れ・整形】\n"
        "1. 発言が途中で遮られる箇所にはダッシュ「——」を挿入してよい\n"
        "2. 句読点が不足している箇所は自然な位置で補ってよい\n"
        "\n【禁止事項】\n"
        "1. 内容の要約・追加・削除は禁止\n"
        "2. 推測による固有名詞の変更は禁止\n"
        "3. 口語表現はそのまま維持する\n"
        + format_hints_for_prompt(filename_hints or [])
        + format_context_for_prompt(job_context or {})
    )


def _strip_speaker_labels(text: str) -> str:
    """行頭の話者ラベル（AI が付けてしまった場合）を除去する。"""
    out_lines: list[str] = []
    for line in text.splitlines():
        cleaned = _SPEAKER_LABEL_LINE_RE.sub("", line)
        out_lines.append(cleaned)
    return "\n".join(out_lines)


def _normalize_turn_breaks(text: str) -> str:
    """段落区切りを正規化する（空行1行、末尾空白除去）。"""
    paragraphs: list[str] = []
    buf: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            if buf:
                paragraphs.append(" ".join(buf).strip())
                buf = []
            continue
        buf.append(line.strip())
    if buf:
        paragraphs.append(" ".join(buf).strip())
    paragraphs = [p for p in paragraphs if p]
    return "\n\n".join(paragraphs)


def _has_turn_breaks(text: str, *, min_paragraphs: int = 3) -> bool:
    """出力に十分な段落分割（話者交代の空行相当）があるか判定する。"""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if len(paragraphs) >= min_paragraphs:
        return True
    # 短文の場合は2段落以上でも OK
    if len(text) < 3000 and len(paragraphs) >= 2:
        return True
    return False


def normalize_turn_segmented_text(text: str) -> str:
    """話者ラベル除去 + 段落正規化を一括適用。"""
    return _normalize_turn_breaks(_strip_speaker_labels(text))


def diarize_transcript(
    text: str,
    *,
    filename_hints: list[str] | None = None,
    job_context: dict[str, Any] | None = None,
    model: str | None = None,
    timeout_sec: int = _TIMEOUT_SEC,
) -> str:
    """補正済みテキストに話者交代箇所の空行（段落分割）を適用する。"""
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
            result = normalize_turn_segmented_text(full_response.strip())
            print(
                f"diarize_speakers_completed attempt={attempt}/{max_attempts} "
                f"output_chars={len(result)} paragraphs={result.count(chr(10)+chr(10))+1} "
                f"elapsed={elapsed:.1f}s",
                flush=True,
            )

            if not result:
                raise ValueError("diarize returned empty output")

            if not _has_turn_breaks(result):
                print(
                    "diarize_speakers_warning: insufficient turn breaks, "
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Step 4.4: 話者交代箇所の空行分割"
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
        ctx_dir = os.path.join(args.input_root, args.job_id)
        job_context = load_job_context(ctx_dir)

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
    print(f"has_turn_breaks={_has_turn_breaks(result)}")


if __name__ == "__main__":
    main()
