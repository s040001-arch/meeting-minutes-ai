"""Rule-enforced span reconstruction (Phase 10.3).

Replaces free-form "rewrite ±1 sentence" LLM prompting, which does not reliably
respect its own span boundaries (observed: 3 of 5 spans overran their window,
2 reverted by the 3b semantic gate). Here the LLM only ever proposes a
*replacement string for one fixed span*; code — never the model — decides
what gets spliced into the transcript, and only after every guard passes.

Pipeline for one call to `reconstruct_span`:
  1. LLM call: model sees target span + confirmed_info + ±1 sentence context
     labelled "reference only, do not include in output"; must return
     ``{"replacement": "..."}`` JSON, nothing else.
  2. Length guard: reject if replacement is more than `max_length_ratio`
     times the original span length (default 3x) — catches the model
     padding in surrounding narrative instead of a minimal edit.
  3. Fact gate: numeric tokens and any `protected_names` present in the
     original span must still be present in the replacement.
  4. Semantic gate (3b): a second, independent LLM call judges whether the
     replacement changes meaning beyond what confirmed_info justifies.

Any failed stage returns ``ok=False`` with a `stage`/`reason`; the caller is
expected to keep the original text and fall back to a ``[補足:]`` annotation.
`apply_span_reconstruction` is the only place that writes into the document,
and it refuses to run on a failed result.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

import anthropic

RECONSTRUCT_MODEL = os.environ.get("RECONSTRUCT_SPAN_MODEL", "claude-opus-5")
SEMANTIC_CHECK_MODEL = os.environ.get("RECONSTRUCT_SPAN_SEMANTIC_MODEL", "claude-sonnet-5")
DEFAULT_MAX_LENGTH_RATIO = 3.0

_NUMBER_RE = re.compile(r"\d+(?:[./]\d+)?")


@dataclass
class SpanReconstructResult:
    ok: bool
    replacement: str | None = None
    reason: str = ""
    stage: str = ""  # "llm_call" | "length_guard" | "fact_gate" | "semantic_gate" | "ok"


def _extract_numbers(text: str) -> set[str]:
    return set(_NUMBER_RE.findall(text or ""))


def _names_present(text: str, names: list[str]) -> set[str]:
    return {n for n in names if n and n in (text or "")}


def _parse_json_object(raw: str) -> dict:
    s = (raw or "").strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    try:
        loaded = json.loads(s)
        if isinstance(loaded, dict):
            return loaded
    except json.JSONDecodeError:
        pass
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        loaded = json.loads(s[start : end + 1])
        if isinstance(loaded, dict):
            return loaded
    raise RuntimeError(f"reconstruct_span JSON parse failed: head={s[:200]!r}")


def _build_reconstruct_prompt(
    *, span_target: str, context_before: str, context_after: str, confirmed_info: str
) -> str:
    return (
        "あなたは日本語の会議逐語録の編集者です。"
        "以下の【対象スパン】だけを、【確定情報】をもとに最小限修正してください。\n\n"
        "厳守事項:\n"
        "- 出力は対象スパンの置き換えテキストのみ。前後文は絶対に出力に含めない。\n"
        "- 【前文脈】【後文脈】は意味を理解するための参考情報。編集禁止・出力に含めない。\n"
        "- 数値・固有名詞・日時は確定情報による追加を除き一切変更しない。\n"
        "- 対象スパンの意味が既に明確な場合は元のテキストのまま返す。\n"
        "- 出力はJSON形式のみ、説明文は不要: {\"replacement\": \"...\"}\n\n"
        f"【前文脈（参照のみ）】\n{context_before}\n\n"
        f"【対象スパン】\n{span_target}\n\n"
        f"【後文脈（参照のみ）】\n{context_after}\n\n"
        f"【確定情報】\n{confirmed_info}\n"
    )


def _call_llm_reconstruct(prompt: str, *, api_key: str) -> str:
    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=RECONSTRUCT_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    parts = [
        str(getattr(b, "text", "") or "")
        for b in getattr(resp, "content", []) or []
        if getattr(b, "type", "") == "text"
    ]
    return "\n".join(parts)


def _build_semantic_system_prompt() -> str:
    return (
        "あなたは議事録編集の監査者です。置換前後で意味が変わっていないか判定してください。"
        "confirmed_info（確定情報）による正当な追加・訂正は許容します。"
        "それ以外の意味変化・事実の書き換え・話者の主張の変質はNGです。"
        '出力はJSONのみ: {"ok": true|false, "reason": "50字以内"}'
    )


def _call_llm_semantic_ok(
    *, span_before: str, replacement: str, confirmed_info: str, api_key: str
) -> tuple[bool, str]:
    payload = json.dumps(
        {"before": span_before, "after": replacement, "confirmed_info": confirmed_info},
        ensure_ascii=False,
    )
    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=SEMANTIC_CHECK_MODEL,
        max_tokens=256,
        timeout=120,
        system=_build_semantic_system_prompt(),
        messages=[{"role": "user", "content": payload}],
    )
    parts = [
        str(getattr(b, "text", "") or "")
        for b in getattr(resp, "content", []) or []
        if getattr(b, "type", "") == "text"
    ]
    parsed = _parse_json_object("\n".join(parts))
    return bool(parsed.get("ok")), str(parsed.get("reason") or "")[:200]


def reconstruct_span(
    *,
    span_target: str,
    context_before: str = "",
    context_after: str = "",
    confirmed_info: str,
    protected_names: list[str] | None = None,
    api_key: str | None = None,
    max_length_ratio: float = DEFAULT_MAX_LENGTH_RATIO,
    skip_semantic_check: bool = False,
) -> SpanReconstructResult:
    """Reconstruct span_target using confirmed_info, with code-enforced guards.

    The LLM never decides what gets written into the document — it only
    proposes `replacement`, which this function validates in three
    independent, fail-closed stages before returning ok=True.
    """
    key = api_key or os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return SpanReconstructResult(ok=False, reason="no_api_key", stage="llm_call")

    prompt = _build_reconstruct_prompt(
        span_target=span_target,
        context_before=context_before,
        context_after=context_after,
        confirmed_info=confirmed_info,
    )
    try:
        raw = _call_llm_reconstruct(prompt, api_key=key)
        parsed = _parse_json_object(raw)
    except Exception as e:  # noqa: BLE001 - fail closed on any LLM/parse error
        return SpanReconstructResult(ok=False, reason=f"llm_call_error:{e}", stage="llm_call")

    replacement = parsed.get("replacement")
    if not isinstance(replacement, str) or not replacement.strip():
        return SpanReconstructResult(
            ok=False, reason="empty_or_missing_replacement", stage="llm_call"
        )

    if len(span_target) > 0 and len(replacement) > len(span_target) * max_length_ratio:
        return SpanReconstructResult(
            ok=False,
            reason=f"length_guard:{len(replacement)}>{max_length_ratio}x{len(span_target)}",
            stage="length_guard",
        )

    missing_nums = _extract_numbers(span_target) - _extract_numbers(replacement)
    if missing_nums:
        return SpanReconstructResult(
            ok=False,
            reason=f"fact_gate:numbers_missing:{sorted(missing_nums)}",
            stage="fact_gate",
        )

    if protected_names:
        missing_names = _names_present(span_target, protected_names) - _names_present(
            replacement, protected_names
        )
        if missing_names:
            return SpanReconstructResult(
                ok=False,
                reason=f"fact_gate:names_missing:{sorted(missing_names)}",
                stage="fact_gate",
            )

    if skip_semantic_check:
        return SpanReconstructResult(
            ok=True, replacement=replacement, reason="ok(semantic_skipped)", stage="ok"
        )

    try:
        sem_ok, sem_reason = _call_llm_semantic_ok(
            span_before=span_target,
            replacement=replacement,
            confirmed_info=confirmed_info,
            api_key=key,
        )
    except Exception as e:  # noqa: BLE001 - fail closed on any LLM/parse error
        return SpanReconstructResult(ok=False, reason=f"semantic_gate_error:{e}", stage="semantic_gate")

    if not sem_ok:
        return SpanReconstructResult(ok=False, reason=f"semantic_gate:{sem_reason}", stage="semantic_gate")

    return SpanReconstructResult(ok=True, replacement=replacement, reason="ok", stage="ok")


def apply_span_reconstruction(text: str, span_start: int, span_end: int, result: SpanReconstructResult) -> str:
    """Splice `result.replacement` into `text[span_start:span_end]`. Refuses failed results."""
    if not result.ok or result.replacement is None:
        raise ValueError("cannot apply a failed reconstruction result")
    if text[span_start:span_end] is None:
        raise ValueError("span out of range")
    return text[:span_start] + result.replacement + text[span_end:]
