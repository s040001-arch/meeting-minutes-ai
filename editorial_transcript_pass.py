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
# Paragraphs are edited in small batches so quality does not depend on
# position: a single long generation degrades toward the end and can even
# truncate, leaving the latter half of the meeting unpolished.
EDITORIAL_BATCH_TARGET_CHARS = 5000
EDITORIAL_BATCH_MAX_PARALLEL = 3
EDITORIAL_REPAIR_MAX_PARALLEL = 3
EDITORIAL_FINDING_RESOLVER_MAX_ITEMS = 20
# Issue phrasings that mean the span blocks reader comprehension.  These are
# handled even at low confidence: confidently fixable ones are resolved in
# place, the rest are asked via LINE instead of being silently kept.
_READER_BLOCKING_ISSUE_RE = re.compile(
    r"意味不明|意味が通らない|崩れ|断片|成立していない|成立しない|文意不明"
)


def is_reader_blocking_finding(finding: dict[str, Any]) -> bool:
    """True if a final-review finding blocks reader comprehension."""
    finding_type = str(finding.get("type") or "").strip().lower()
    if finding_type not in {"unnatural", "fragment"}:
        return False
    confidence = str(finding.get("confidence") or "").strip().lower()
    if confidence in {"high", "medium"}:
        return True
    return bool(
        _READER_BLOCKING_ISSUE_RE.search(str(finding.get("issue") or ""))
    )
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
    # Equal multisets mean no token was altered.  Positional rewriting would
    # corrupt legitimate sentence reordering (swapping values between
    # clauses), so restore only actual token drift.
    if Counter(source_tokens) == Counter(m.group(0) for m in matches):
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
        "\n\n【出力形式】入力の全indexを保持したJSON配列のみ。"
        '各要素は {"index":0,"text":"整文後の段落"} とする。'
        "説明、Markdownコードフェンスは禁止。"
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


def _parse_paragraph_array(raw: str) -> dict[int, str]:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    parsed = json.loads(text)
    if not isinstance(parsed, list):
        raise ValueError("editorial response is not an array")
    out: dict[int, str] = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        value = item.get("text")
        if isinstance(value, str) and value.strip():
            out[index] = value.strip()
    if not out:
        raise ValueError("editorial response has no indexed paragraphs")
    return out


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
    except Exception as exc:  # noqa: BLE001
        stats["failed"] = True
        stats["validation_errors"] = [f"request_failed:{exc!r}"]
        return text, stats
    system = _build_system_prompt(meeting_profile)

    # Batch paragraphs so no request produces a long generation.  Quality
    # must not depend on where in the meeting a paragraph appears.
    batches: list[list[int]] = []
    batch: list[int] = []
    batch_chars = 0
    for index, paragraph in enumerate(before_paragraphs):
        if batch and batch_chars + len(paragraph) > EDITORIAL_BATCH_TARGET_CHARS:
            batches.append(batch)
            batch = []
            batch_chars = 0
        batch.append(index)
        batch_chars += len(paragraph)
    if batch:
        batches.append(batch)
    stats["total_batches"] = len(batches)

    def edit_batch(indices: list[int]) -> dict[int, str]:
        payload = [
            {"index": index, "text": before_paragraphs[index]}
            for index in indices
        ]
        response = client.messages.create(
            model=stats["model"],
            max_tokens=EDITORIAL_MAX_TOKENS,
            system=system,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "以下の発言録段落配列を完成稿にしてください。\n\n"
                        + json.dumps(payload, ensure_ascii=False)
                    ),
                }
            ],
        )
        return _parse_paragraph_array(_extract_response_text(response))

    after_by_index: dict[int, str] = {}
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=EDITORIAL_BATCH_MAX_PARALLEL
    ) as executor:
        future_to_batch = {
            executor.submit(edit_batch, indices): position
            for position, indices in enumerate(batches)
        }
        for future in concurrent.futures.as_completed(future_to_batch):
            position = future_to_batch[future]
            try:
                after_by_index.update(future.result())
            except Exception as exc:  # noqa: BLE001
                # A failed batch falls back paragraph by paragraph below;
                # the rest of the transcript is still polished.
                stats["validation_errors"].append(
                    f"batch_{position}_failed:{exc!r}"
                )

    selected: list[str] = list(before_paragraphs)
    repair_needed: dict[int, tuple[str, str, list[str]]] = {}
    for index, before in enumerate(before_paragraphs):
        after = after_by_index.get(index)
        if after is None:
            stats["fallback_chunk_idx"].append(index)
            stats["validation_errors"].append(
                f"paragraph_{index}:missing_from_response"
            )
            continue
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
        # An already-clean transcript needing no edits is a success; only
        # treat zero applied paragraphs as failure when validation errors
        # forced fallbacks.
        if stats["fallback_chunk_idx"] or stats["validation_errors"]:
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


def resolve_reader_blocking_findings(
    *,
    text: str,
    findings: list[dict[str, Any]],
    meeting_profile: dict[str, Any] | None = None,
    force: bool = False,
    max_items: int | None = None,
    extra_knowledge: str = "",
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve non-factual medium garbles after the independent final review.

    force=True は統合仕上げパス用で、EDITORIAL_TRANSCRIPT_ENABLED に
    依存せず修復を実行する。
    """
    if not force and not is_editorial_transcript_enabled():
        return text, [], []
    item_cap = max_items or EDITORIAL_FINDING_RESOLVER_MAX_ITEMS
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return text, [], [{"reason": "anthropic_api_key_missing"}]

    candidates: list[dict[str, Any]] = []
    for finding in findings:
        quote = str(finding.get("quote") or "").strip()
        issue = str(finding.get("issue") or "").strip()
        if not quote or quote not in text:
            continue
        if not is_reader_blocking_finding(finding):
            continue
        if any(
            marker in issue
            for marker in ("人名", "数値", "表記ゆれ", "固有名詞", "同一人物")
        ):
            continue
        position = text.find(quote)
        candidates.append(
            {
                "index": len(candidates),
                "quote": quote,
                "issue": issue,
                "context": text[
                    max(0, position - 220) : position + len(quote) + 220
                ],
            }
        )
        if len(candidates) >= item_cap:
            break
    if not candidates:
        return text, [], []

    system = (
        "あなたは議事録の最終整文で残った、読者の理解を妨げる崩れだけを修復します。"
        "最優先は、後で読む人がストレスなく理解できることです。逐語の忠実さより優先します。"
        "各項目のquoteを置換する自然なreplacementをJSON配列で返してください。"
        "\n- 前後の文脈から確実に言える場合のみ置換する。一般化してよいが、"
        "推測で新しい具体情報を作らない。"
        "\n- 主張・理由・事実は残し、人名・数値・固有名詞を変更・削除・追加しない。"
        "\n- 本当に判断できない場合は replacement を空文字にする。"
        "その項目は自動処理せず、内容を知る担当者へ質問として送られる。"
        '\n出力形式: [{"index":0,"replacement":"..."}] のみ。'
    )
    if extra_knowledge.strip():
        # 担当者が確定した回答は最優先の事実。序盤の回答で後半の同種の
        # 崩れが推測可能になる（カスケード解決）。
        system = system + "\n\n" + extra_knowledge.strip()
    try:
        client = anthropic.Anthropic(api_key=api_key, timeout=EDITORIAL_TIMEOUT_SEC)
        response = client.messages.create(
            model=resolve_editorial_model(),
            max_tokens=8000,
            system=system,
            messages=[
                {
                    "role": "user",
                    "content": json.dumps(candidates, ensure_ascii=False),
                }
            ],
        )
        raw = _extract_response_text(response)
        parsed = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        return text, [], [{"reason": f"resolver_request_failed:{exc!r}"}]
    if not isinstance(parsed, list):
        return text, [], [{"reason": "resolver_output_not_list"}]

    by_index: dict[int, str] = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        by_index[index] = str(item.get("replacement") or "").strip()
    out = text
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for candidate in candidates:
        quote = candidate["quote"]
        replacement = by_index.get(candidate["index"], "")
        if not replacement or replacement == quote or quote not in out:
            # Unresolved garble is escalated to a LINE question by the
            # quality gate; it is never silently kept or deleted.
            skipped.append({**candidate, "reason": "empty_or_not_present"})
            continue
        before_numbers, before_names = _protected_multiset(quote)
        after_numbers, after_names = _protected_multiset(replacement)
        if before_numbers != after_numbers or before_names != after_names:
            skipped.append({**candidate, "reason": "protected_tokens_changed"})
            continue
        if not (0.2 <= len(replacement) / max(1, len(quote)) <= 2.5):
            skipped.append({**candidate, "reason": "replacement_ratio"})
            continue
        proposed = out.replace(quote, replacement, 1)
        integrity = verify_fact_integrity(
            out,
            proposed,
            meeting_profile=meeting_profile,
        )
        if not integrity.ok:
            skipped.append(
                {
                    **candidate,
                    "reason": "fact_integrity",
                    "violations": integrity.violations,
                }
            )
            continue
        out = proposed
        applied.append(
            {
                "type": "editorial_resolver",
                "quote": quote,
                "issue": candidate["issue"],
                "fix": replacement,
                "confidence": "high",
            }
        )
    return out, applied, skipped
