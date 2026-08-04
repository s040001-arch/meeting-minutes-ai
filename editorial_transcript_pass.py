"""Reader-facing full-transcript editorial pass with paragraph fact fallback."""
from __future__ import annotations

from collections import Counter
import concurrent.futures
import json
import os
import re
from typing import Any

import anthropic

from anthropic_prompt_cache import OPUS_MODEL_ID, cached_system
from fact_integrity_gate import verify_fact_integrity
from meeting_profile import format_meeting_profile_for_prompt

EDITORIAL_TRANSCRIPT_FILENAME = "merged_transcript_editorial.txt"
EDITORIAL_TRANSCRIPT_MODEL = OPUS_MODEL_ID
EDITORIAL_MAX_TOKENS = 32000
EDITORIAL_TIMEOUT_SEC = 900
EDITORIAL_CHAR_CAP = 60_000
EDITORIAL_REPAIR_MAX_PARALLEL = 3
_NUMBER_RE = re.compile(
    r"(?:\d+(?:[.,、〜～-]\d+)*(?:kg|人|店|店舗|日|ヶ月|月|年|行|割|回|社|"
    r"時|分|万円|円|%|クラス|名)|\d{2,}(?:[.,、〜～-]\d+)*)",
    re.IGNORECASE,
)
_HONORIFIC_NAME_RE = re.compile(r"[一-龥ァ-ヶA-Za-z]{1,12}(?:さん|様)")
_DOMAIN_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:THR|PLS|ChatGPT|Gemini|Claude|Slack|Teams|"
    r"Outlook|SAP|DX|AI|SV|KPI|PFC|A3)(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def is_editorial_transcript_enabled() -> bool:
    raw = os.environ.get("EDITORIAL_TRANSCRIPT_ENABLED", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def resolve_editorial_model() -> str:
    return (
        os.environ.get("EDITORIAL_TRANSCRIPT_MODEL", "").strip()
        or EDITORIAL_TRANSCRIPT_MODEL
    )


def editorial_transcript_path(job_dir: str) -> str:
    return os.path.join(job_dir, EDITORIAL_TRANSCRIPT_FILENAME)


def _protected_multiset(text: str) -> tuple[Counter[str], Counter[str]]:
    return Counter(_NUMBER_RE.findall(text)), Counter(_HONORIFIC_NAME_RE.findall(text))


def _restore_ordered_tokens(
    source: str,
    candidate: str,
    pattern: re.Pattern[str],
) -> tuple[str, bool]:
    source_tokens = pattern.findall(source)
    matches = list(pattern.finditer(candidate))
    if len(source_tokens) != len(matches):
        return candidate, False
    replacements = [
        (match.start(), match.end(), source_tokens[index])
        for index, match in enumerate(matches)
        if match.group(0) != source_tokens[index]
    ]
    out = candidate
    for start, end, token in reversed(replacements):
        out = out[:start] + token + out[end:]
    return out, bool(replacements)


def _build_system_prompt(
    meeting_profile: dict[str, Any] | None,
) -> str | list:
    profile_block = format_meeting_profile_for_prompt(meeting_profile or {})
    world_block = ""
    try:
        from world_knowledge_store import get_runtime_knowledge_block

        world_block = get_runtime_knowledge_block(
            meeting_profile=meeting_profile or {},
            purpose="coherence",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"editorial_world_knowledge_failed={exc!r}")

    static_prompt = (
        "あなたは企業会議の「発言録（整文）」の最終編集者です。"
        "これは逐語再現ではなく、読者が内容を正確かつ自然に追える整文版です。"
        "全文を一読し、音声認識の崩れ、相槌の混入、言い直し、意味不明な断片、"
        "不自然な文法を残さない完成稿にしてください。"
        "\n\n【必須】"
        "\n- 主張、理由、事例、決定、アクション、明確な固有名詞は維持し、新しい事実を作らない。"
        "\n- 文脈から一意な一般語の誤認識は自然に修正する。"
        "\n- 正確に復元できない低価値の崩れ片は削除する。内容上必要だが細部だけ不明なら、"
        "確実に言える範囲へ一般化する。推測の固有名詞は作らない。"
        "\n- 挨拶、単独相槌、フィラー、重複を圧縮し、文を適切に分割・接続する。"
        "\n- 要約、箇条書き、話者名の創作、注釈、[要確認]の新設、前置きは禁止。"
        "発言録本文だけを出力する。"
        "\n- 入力の段落数・段落順は必ず維持する。段落の結合・分割・並べ替えは禁止。"
        "各入力段落に対して、対応する出力段落を一つだけ返す。"
        "\n\n【出力形式】入力と同じ要素数・順序のJSON文字列配列のみ。"
        "説明、キー付きオブジェクト、Markdownコードフェンスは禁止。"
    )
    variable = "\n\n".join(
        block for block in (profile_block, world_block) if block.strip()
    )
    return cached_system(static_prompt, variable)


def _extract_response_text(response: Any) -> str:
    return "".join(
        str(block.text)
        for block in response.content
        if getattr(block, "type", "") == "text"
    ).strip()


def _parse_paragraph_array(raw: str) -> list[str]:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    parsed = json.loads(text)
    if not isinstance(parsed, list) or not all(
        isinstance(item, str) for item in parsed
    ):
        raise ValueError("editorial response is not a string array")
    return [item.strip() for item in parsed]


def _split_paragraphs(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"\n\s*\n+", text) if part.strip()]


def _validate_editorial_paragraph(
    before: str,
    candidate: str,
    meeting_profile: dict[str, Any] | None,
) -> list[str]:
    errors: list[str] = []
    ratio = len(candidate) / max(1, len(before))
    if not (0.4 <= ratio <= 1.4):
        errors.append(f"output_ratio:{ratio:.3f}")
    before_numbers, before_names = _protected_multiset(before)
    after_numbers, after_names = _protected_multiset(candidate)
    if before_numbers != after_numbers:
        errors.append("numeric_tokens_changed")
    if before_names != after_names:
        errors.append("honorific_names_changed")
    before_domain = {token.lower() for token in _DOMAIN_TOKEN_RE.findall(before)}
    after_domain = {token.lower() for token in _DOMAIN_TOKEN_RE.findall(candidate)}
    if before_domain != after_domain:
        errors.append("domain_tokens_changed")
    integrity = verify_fact_integrity(
        before,
        candidate,
        meeting_profile=meeting_profile,
    )
    errors.extend(f"fact_integrity:{item}" for item in integrity.violations)
    return errors


def editorialize_transcript(
    text: str,
    meeting_profile: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    stats: dict[str, Any] = {
        "enabled": is_editorial_transcript_enabled(),
        "attempted": False,
        "applied": False,
        "failed": False,
        "model": resolve_editorial_model(),
        "input_chars": len(text),
        "output_chars": len(text),
        "validation_errors": [],
        "total_paragraphs": 0,
        "applied_paragraphs": 0,
        "fallback_chunk_idx": [],
        "restored_token_paragraphs": [],
        "repaired_paragraphs": [],
    }
    if not stats["enabled"]:
        return text, stats
    if not text.strip() or len(text) > EDITORIAL_CHAR_CAP:
        stats["failed"] = True
        stats["validation_errors"] = ["empty_or_over_char_cap"]
        return text, stats

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        stats["failed"] = True
        stats["validation_errors"] = ["anthropic_api_key_missing"]
        return text, stats

    stats["attempted"] = True
    before_paragraphs = _split_paragraphs(text)
    stats["total_paragraphs"] = len(before_paragraphs)
    try:
        client = anthropic.Anthropic(api_key=api_key, timeout=EDITORIAL_TIMEOUT_SEC)
        response = client.messages.create(
            model=stats["model"],
            max_tokens=EDITORIAL_MAX_TOKENS,
            system=_build_system_prompt(meeting_profile),
            messages=[
                {
                    "role": "user",
                    "content": (
                        "以下の発言録段落配列を完成稿にしてください。\n\n"
                        + json.dumps(before_paragraphs, ensure_ascii=False)
                    ),
                }
            ],
        )
        raw = _extract_response_text(response)
        after_paragraphs = _parse_paragraph_array(raw)
    except Exception as exc:  # noqa: BLE001
        stats["failed"] = True
        stats["validation_errors"] = [f"request_failed:{exc!r}"]
        return text, stats

    if len(after_paragraphs) != len(before_paragraphs):
        stats["failed"] = True
        stats["validation_errors"] = [
            f"paragraph_count:{len(before_paragraphs)}->{len(after_paragraphs)}"
        ]
        return text, stats

    selected: list[str] = list(before_paragraphs)
    repair_needed: dict[int, tuple[str, str, list[str]]] = {}
    for index, (before, after) in enumerate(
        zip(before_paragraphs, after_paragraphs, strict=True)
    ):
        repaired, restored_numbers = _restore_ordered_tokens(
            before,
            after,
            _NUMBER_RE,
        )
        repaired, restored_names = _restore_ordered_tokens(
            before,
            repaired,
            _HONORIFIC_NAME_RE,
        )
        if restored_numbers or restored_names:
            stats["restored_token_paragraphs"].append(index)
        errors = _validate_editorial_paragraph(
            before,
            repaired,
            meeting_profile,
        )
        if errors:
            repair_needed[index] = (before, repaired, errors)
            continue
        selected[index] = repaired
        if repaired.strip() != before.strip():
            stats["applied_paragraphs"] += 1

    repair_system = (
        "あなたは議事録の1段落だけを事実安全に修復する編集者です。"
        "原文の人名・数値・固有略称を一つも削除・変更せず、編集案の読みやすさを維持して、"
        "音声認識の崩れ・重複・フィラーを除いた自然な1段落を返してください。"
        "新しい事実、人名、数値は追加しない。出力は段落本文のみ。"
    )

    def repair_paragraph(
        index: int,
        before: str,
        draft: str,
    ) -> tuple[int, str | None, list[str]]:
        protected = {
            "numbers": _NUMBER_RE.findall(before),
            "names": _HONORIFIC_NAME_RE.findall(before),
            "domain_tokens": _DOMAIN_TOKEN_RE.findall(before),
        }
        try:
            response = client.messages.create(
                model=stats["model"],
                max_tokens=4000,
                system=repair_system,
                messages=[
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "original": before,
                                "edited_draft": draft,
                                "must_preserve_exactly": protected,
                            },
                            ensure_ascii=False,
                        ),
                    }
                ],
            )
            candidate = _extract_response_text(response)
        except Exception as exc:  # noqa: BLE001
            return index, None, [f"repair_request_failed:{exc!r}"]
        candidate, _ = _restore_ordered_tokens(before, candidate, _NUMBER_RE)
        candidate, _ = _restore_ordered_tokens(
            before,
            candidate,
            _HONORIFIC_NAME_RE,
        )
        errors = _validate_editorial_paragraph(
            before,
            candidate,
            meeting_profile,
        )
        return index, (candidate if not errors else None), errors

    if repair_needed:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=EDITORIAL_REPAIR_MAX_PARALLEL
        ) as executor:
            futures = [
                executor.submit(repair_paragraph, index, before, draft)
                for index, (before, draft, _errors) in repair_needed.items()
            ]
            repaired_results = {
                result[0]: result
                for result in (
                    future.result()
                    for future in concurrent.futures.as_completed(futures)
                )
            }
        for index, (before, _draft, initial_errors) in repair_needed.items():
            _idx, repaired, repair_errors = repaired_results[index]
            if repaired is None:
                stats["fallback_chunk_idx"].append(index)
                stats["validation_errors"].extend(
                    f"paragraph_{index}:{error}"
                    for error in (initial_errors + repair_errors)
                )
                continue
            selected[index] = repaired
            stats["repaired_paragraphs"].append(index)
            if repaired.strip() != before.strip():
                stats["applied_paragraphs"] += 1

    candidate = "\n\n".join(selected)
    stats["output_chars"] = len(candidate)
    stats["applied"] = candidate.strip() != text.strip()
    # Paragraphs with protected-fact changes fall back independently. The
    # final reviewer audits both rewritten and fallback paragraphs.
    if stats["applied_paragraphs"] == 0:
        stats["failed"] = True
        return text, stats
    return candidate.strip() + "\n", stats


def generate_editorial_transcript(
    *,
    job_dir: str,
    text: str,
    meeting_profile: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any], str]:
    editorial, stats = editorialize_transcript(text, meeting_profile)
    path = editorial_transcript_path(job_dir)
    os.makedirs(job_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(editorial)
    print(
        "editorial_transcript="
        f'{{"enabled":{str(stats["enabled"]).lower()},'
        f'"applied":{str(stats["applied"]).lower()},'
        f'"failed":{str(stats["failed"]).lower()},'
        f'"input_chars":{stats["input_chars"]},'
        f'"output_chars":{stats["output_chars"]}}}'
    )
    return editorial, stats, path
