#!/usr/bin/env python3
"""Non-publishing shadow editor for transcript architecture experiments.

This path intentionally bypasses mechanical corrections, learned replacements,
auto-deletion, question mutation, and Google Docs export.  It reads the merged
raw transcript once, writes one shadow artifact, and never updates production
job state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import anthropic

from anthropic_prompt_cache import OPUS_MODEL_ID, cached_system


DEFAULT_INPUT = "merged_transcript.txt"
DEFAULT_MODEL = OPUS_MODEL_ID
MAX_INPUT_CHARS = 80_000
# Claude Opus 5 may consume a substantial part of max_tokens in adaptive
# reasoning before emitting a long Japanese transcript.  32k caused a real
# 21k-character THR shadow to stop after only 923 output characters.
MAX_OUTPUT_TOKENS = 128_000
MAX_ANSWER_CONTEXT_CHARS = 24_000
MIN_OUTPUT_LENGTH_RATIO = 0.70

_SYSTEM = """\
あなたは、会議に出ていない読者のために「会話調の整文記録」を作る編集者です。

【完成稿の定義】
- 発言の論点・理由・具体例・条件・ニュアンスは省略しない。
- 要約ではない。会話の細かな内容と、話している感じは残す。
- 一方で、意味を足さないフィラー、相槌、吃音、重複、言い直し残骸は整理する。
- 口語特有の長い文や壊れた接続は、初見の読者が自然に理解できる文へ整える。
- 文脈から確実に言える範囲では、穏当な言い換え・主語や接続の補完をしてよい。
- 音声認識が壊れていても、意味不明な文字列をそのまま完成稿に残さない。

【事実の扱い】
- 人名、会社名、製品名、数値、金額、日時、決定、否定・肯定、発言主体を推測で作らない。
- 全文の別箇所に十分な根拠がある場合は、表記揺れや明白な音声誤認識を統一してよい。
- 文脈から一意に決まらない事実は、無理に復元せず
  「[要確認: 原文『…』]」の形で最小範囲だけ残す。
- 入力にない新しい主張・理由・結論を追加しない。

【文章の形】
- 話者名は推測して付けない。話者交代は空行で表現する。
- 説明を箇条書きや要約へ変換せず、会話の順序を維持する。
- 出力は整文後の本文だけ。前置き、注釈、作業説明、Markdown見出しは禁止。
- 冒頭から末尾まで同じ注意密度で処理し、途中で省略しない。
"""


def _safe_profile(job_dir: Path) -> dict[str, Any]:
    """Return only user/job-provided context, never learned correction hints."""
    path = job_dir / "meeting_profile.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    allowed = (
        "title",
        "company",
        "participants",
        "participant_names",
        "meeting_date",
        "agenda",
        "prior_context",
        "prior_context_summary",
        "filename_hints",
    )
    return {key: raw[key] for key in allowed if raw.get(key)}


def _job_answer_context(job_dir: Path) -> list[dict[str, str]]:
    """Load only human-confirmed answers from this job.

    Cross-job learned corrections, auto-applied answers, and inferred knowledge
    are deliberately excluded.  This separates useful conversational memory
    from autonomous learning during the shadow experiment.
    """
    path = job_dir / "unknown_points.json"
    if not path.is_file():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(rows, list):
        return []
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    used_chars = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("status") != "answered" or row.get("auto_applied"):
            continue
        answer = str(row.get("answer") or "").strip()
        if not answer:
            continue
        quote = str(
            row.get("span_text")
            or row.get("text")
            or row.get("anomaly_word")
            or ""
        ).strip()
        candidate = str(
            row.get("estimated_correction")
            or row.get("hypothesis")
            or row.get("span_corrected")
            or ""
        ).strip()
        if not quote:
            continue
        item = {
            "原文箇所": quote[:240],
            "提示候補": candidate[:240],
            "ユーザー回答": answer[:400],
        }
        key = (item["原文箇所"], item["提示候補"], item["ユーザー回答"])
        if key in seen:
            continue
        size = sum(len(value) for value in item.values())
        if used_chars + size > MAX_ANSWER_CONTEXT_CHARS:
            break
        seen.add(key)
        result.append(item)
        used_chars += size
    return result


def _response_text(response: Any) -> str:
    return "".join(
        str(getattr(block, "text", "") or "")
        for block in (getattr(response, "content", None) or [])
        if getattr(block, "type", "") == "text"
    ).strip()


def edit_transcript_once(
    source: str,
    *,
    job_dir: Path,
    model: str = DEFAULT_MODEL,
    include_job_answers: bool = True,
) -> tuple[str, dict[str, Any]]:
    """Edit one full transcript and return text plus completion metadata.

    This is the shared core for production and shadow execution.  It does not
    write files or mutate question/learning state.
    """
    source = source.strip()
    if not source:
        raise ValueError("empty input")
    if len(source) > MAX_INPUT_CHARS:
        raise ValueError(
            f"input too large for single-pass editor: {len(source)} > "
            f"{MAX_INPUT_CHARS}"
        )
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    profile = _safe_profile(job_dir)
    variable = ""
    if profile:
        variable = (
            "\n\n【この会議について（ファイル名・ユーザー提供情報のみ）】\n"
            + json.dumps(profile, ensure_ascii=False)
        )
    answers = _job_answer_context(job_dir) if include_job_answers else []
    if answers:
        variable += (
            "\n\n【この会議でユーザー本人が既に確定した回答】\n"
            "以下はこのジョブ内の質問に対するユーザー回答であり、"
            "外部の学習辞書ではありません。回答が「はい」「正しい」等の場合は"
            "提示候補を承認した意味です。同じ内容の箇所にも一貫して反映してください。"
            "回答と矛盾する推測はしないでください。\n"
            + json.dumps(answers, ensure_ascii=False)
        )
    started = time.monotonic()
    client = anthropic.Anthropic(api_key=api_key, timeout=1200)
    response = client.messages.create(
        model=model,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=cached_system(_SYSTEM, variable),
        messages=[{"role": "user", "content": source}],
    )
    output = _response_text(response)
    if not output:
        raise RuntimeError("single-pass editor returned empty output")
    stop_reason = getattr(response, "stop_reason", None)
    length_ratio = len(output) / len(source)
    complete = (
        stop_reason != "max_tokens"
        and length_ratio >= MIN_OUTPUT_LENGTH_RATIO
    )
    meta = {
        "model": model,
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "source_chars": len(source),
        "output_chars": len(output),
        "length_ratio": round(length_ratio, 4),
        "elapsed_sec": round(time.monotonic() - started, 1),
        "stop_reason": stop_reason,
        "complete": complete,
        "profile_keys_used": sorted(profile),
        "job_answers_used": len(answers),
    }
    return output, meta


def run_shadow(
    *,
    job_dir: Path,
    model: str,
    input_name: str = DEFAULT_INPUT,
    include_job_answers: bool = False,
) -> tuple[Path, Path]:
    source_path = job_dir / input_name
    source = source_path.read_text(encoding="utf-8").strip()
    output, core_meta = edit_transcript_once(
        source,
        job_dir=job_dir,
        model=model,
        include_job_answers=include_job_answers,
    )
    complete = bool(core_meta["complete"])

    slug = model.replace("/", "_").replace(":", "_")
    if include_job_answers:
        slug += "_job-answers"
    suffix = "" if complete else "_incomplete"
    output_path = job_dir / f"shadow_single_pass_{slug}{suffix}.txt"
    report_path = job_dir / f"shadow_single_pass_{slug}.json"
    output_path.write_text(output + "\n", encoding="utf-8")
    report = {
        "shadow_only": True,
        "published": False,
        "line_or_question_state_changed": False,
        "learning_state_changed": False,
        **core_meta,
        "source": input_name,
        "output_file": output_path.name,
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if not complete:
        raise RuntimeError(
            "shadow editor output incomplete: "
            f"stop_reason={core_meta['stop_reason']} "
            f"length_ratio={core_meta['length_ratio']:.3f}; "
            f"saved={output_path}"
        )
    return output_path, report_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-dir", required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--input-name", default=DEFAULT_INPUT)
    parser.add_argument("--include-job-answers", action="store_true")
    args = parser.parse_args()
    output, report = run_shadow(
        job_dir=Path(args.job_dir),
        model=args.model,
        input_name=args.input_name,
        include_job_answers=args.include_job_answers,
    )
    print(f"shadow_output={output}")
    print(f"shadow_report={report}")


if __name__ == "__main__":
    main()
