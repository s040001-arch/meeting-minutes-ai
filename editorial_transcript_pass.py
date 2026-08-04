"""Reader-facing full-transcript editorial pass with locked fact tokens."""
from __future__ import annotations

from collections import Counter
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
EDITORIAL_MIN_OUTPUT_RATIO = 0.65
EDITORIAL_MAX_OUTPUT_RATIO = 1.15

_NUMBER_RE = re.compile(
    r"\d+(?:[.,、〜～-]\d+)*(?:kg|人|店|店舗|日|ヶ月|月|年|行|割|回|社|"
    r"時|分|万円|円|%|クラス|名)?",
    re.IGNORECASE,
)
_HONORIFIC_NAME_RE = re.compile(r"[一-龥ァ-ヶA-Za-z]{1,12}(?:さん|様)")
_VERIFY_FRAGMENT_RE = re.compile(r"[^\n。]{0,50}\[要確認\]")
_DOMAIN_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:THR|PLS|ChatGPT|Gemini|Claude|Slack|Teams|"
    r"Outlook|SAP|DX|AI|SV|KPI|PFC|A3)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_LOCK_PLACEHOLDER_RE = re.compile(r"⟦LOCK\d{4}⟧")


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


def _profile_lock_tokens(meeting_profile: dict[str, Any] | None) -> set[str]:
    profile = meeting_profile or {}
    tokens: set[str] = set()
    for key in ("participants", "attendees", "customer_names"):
        for value in profile.get(key) or []:
            token = str(value or "").strip()
            if token:
                tokens.add(token)
    return tokens


def _lock_spans(
    text: str,
    meeting_profile: dict[str, Any] | None,
) -> tuple[str, dict[str, str]]:
    spans: list[tuple[int, int]] = []
    for pattern in (
        _VERIFY_FRAGMENT_RE,
        _NUMBER_RE,
        _HONORIFIC_NAME_RE,
        _DOMAIN_TOKEN_RE,
    ):
        spans.extend((m.start(), m.end()) for m in pattern.finditer(text))
    for token in _profile_lock_tokens(meeting_profile):
        spans.extend((m.start(), m.end()) for m in re.finditer(re.escape(token), text))

    selected: list[tuple[int, int]] = []
    for start, end in sorted(spans, key=lambda item: (item[0], -(item[1] - item[0]))):
        if selected and start < selected[-1][1]:
            continue
        selected.append((start, end))

    mapping: dict[str, str] = {}
    parts: list[str] = []
    cursor = 0
    for index, (start, end) in enumerate(selected):
        key = f"⟦LOCK{index:04d}⟧"
        mapping[key] = text[start:end]
        parts.extend((text[cursor:start], key))
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts), mapping


def _unlock_text(text: str, mapping: dict[str, str]) -> tuple[str, list[str]]:
    errors: list[str] = []
    out = text
    for key, token in mapping.items():
        count = out.count(key)
        if count != 1:
            errors.append(f"placeholder_count:{key}:{count}")
            continue
        out = out.replace(key, token)
    unknown = sorted(set(_LOCK_PLACEHOLDER_RE.findall(out)) - set(mapping))
    if unknown:
        errors.append(f"unknown_placeholders:{unknown[:10]}")
    return out, errors


def _protected_multiset(text: str) -> tuple[Counter[str], Counter[str]]:
    return Counter(_NUMBER_RE.findall(text)), Counter(_HONORIFIC_NAME_RE.findall(text))


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
        "\n- ⟦LOCK0000⟧ 形式のトークンは、順序・個数・表記を一切変えず必ずそのまま出力する。"
        "これは人名・数値・確認済み事実を保護するためのトークンである。"
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
    locked, mapping = _lock_spans(text, meeting_profile)
    try:
        client = anthropic.Anthropic(api_key=api_key, timeout=EDITORIAL_TIMEOUT_SEC)
        response = client.messages.create(
            model=stats["model"],
            max_tokens=EDITORIAL_MAX_TOKENS,
            system=_build_system_prompt(meeting_profile),
            messages=[
                {
                    "role": "user",
                    "content": "以下の発言録を完成稿にしてください。\n\n" + locked,
                }
            ],
        )
        raw = _extract_response_text(response)
    except Exception as exc:  # noqa: BLE001
        stats["failed"] = True
        stats["validation_errors"] = [f"request_failed:{exc!r}"]
        return text, stats

    candidate, errors = _unlock_text(raw, mapping)
    ratio = len(candidate) / max(1, len(text))
    if not (EDITORIAL_MIN_OUTPUT_RATIO <= ratio <= EDITORIAL_MAX_OUTPUT_RATIO):
        errors.append(f"output_ratio:{ratio:.3f}")
    before_numbers, before_names = _protected_multiset(text)
    after_numbers, after_names = _protected_multiset(candidate)
    if before_numbers != after_numbers:
        errors.append("numeric_tokens_changed")
    if before_names != after_names:
        errors.append("honorific_names_changed")
    integrity = verify_fact_integrity(
        text,
        candidate,
        meeting_profile=meeting_profile,
    )
    errors.extend(f"fact_integrity:{item}" for item in integrity.violations)
    stats["validation_errors"] = errors
    stats["output_chars"] = len(candidate)
    if errors:
        stats["failed"] = True
        return text, stats

    stats["applied"] = candidate.strip() != text.strip()
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
