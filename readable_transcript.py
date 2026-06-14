"""Readable transcript pass: conservative cleanup for Hub Doc minutes.

Reads merged_transcript_after_qa.txt (or caller-supplied source text) and writes
merged_transcript_readable.txt. Source files are never modified.
"""
from __future__ import annotations

import concurrent.futures
import os
import re
from typing import Any

import anthropic

from anthropic_prompt_cache import cached_system
from meeting_profile import format_meeting_profile_for_prompt

READABLE_TRANSCRIPT_FILENAME = "merged_transcript_readable.txt"
READABLE_MODEL = "claude-sonnet-4-20250514"
READABLE_MAX_TOKENS = 8192
READABLE_TIMEOUT_SEC = 180
READABLE_CHUNK_TARGET_CHARS = 3500
READABLE_CHUNK_MIN_CHARS = 800
READABLE_MAX_PARALLEL = 4
READABLE_MIN_OUTPUT_RATIO = 0.25

_HEADING_LINE_RE = re.compile(r"^(?:###\s*)?▼")
_PARAGRAPH_SEP = re.compile(r"\n\s*\n+")
_FLAGGED_TOKEN_RE = re.compile(r"\[要確認\]")


def _shield_flagged_tokens(text: str) -> tuple[str, dict[str, str]]:
    """Replace each tagged span with opaque placeholders for LLM safety."""
    mapping: dict[str, str] = {}
    parts: list[str] = []
    last = 0
    for m in _FLAGGED_TOKEN_RE.finditer(text):
        left_start = max(0, m.start() - 50)
        left = text[left_start : m.start()]
        seg_start = max(left.rfind("\n"), left.rfind("。")) + 1
        frag_start = left_start + seg_start
        token = text[frag_start : m.end()]
        key = f"⟦FLAG{len(mapping)}⟧"
        mapping[key] = token
        parts.append(text[last:frag_start])
        parts.append(key)
        last = m.end()
    parts.append(text[last:])
    return "".join(parts), mapping


def _unshield_flagged_tokens(text: str, mapping: dict[str, str]) -> str:
    out = text
    for key, token in mapping.items():
        out = out.replace(key, token)
    return out


def _extract_flagged_tokens(text: str) -> list[str]:
    """Return tagged fragments; prefer short tail before each [要確認]."""
    tokens: list[str] = []
    for m in _FLAGGED_TOKEN_RE.finditer(text):
        left = text[max(0, m.start() - 80) : m.start()]
        frag = re.split(r"[\n。]", left)[-1]
        tokens.append(f"{frag}[要確認]")
    return tokens


def is_readable_transcript_enabled() -> bool:
    raw = os.environ.get("READABLE_TRANSCRIPT_ENABLED", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def readable_transcript_path(job_dir: str) -> str:
    return os.path.join(job_dir, READABLE_TRANSCRIPT_FILENAME)


def _build_system_prompt(meeting_profile: dict[str, Any] | None) -> str | list:
    profile_block = format_meeting_profile_for_prompt(meeting_profile or {})
    static_prompt = (
        "あなたは議事録の発言録を「読みやすく整える」担当です。"
        "入力テキストは逐語録（Q&A反映後）です。内容の意味・事実は変えず、"
        "読みやすさのためだけに無内容部分を圧縮・整理してください。"
        "\n\n【圧縮・除去してよいもの】"
        "\n- 冒頭/締めの挨拶連呼（ありがとうございました、お疲れ様です 等）"
        "\n- 内容のない相槌（はい、なるほど 等の単独・連続）"
        "\n- 言い淀み・言い直し・フィラー（えーと、あの、まあ、ちょっと 等）"
        "\n- 単純な言い直しによる重複（同じ趣旨の言い直しで片方が明らかに冗長な場合）"
        "\n- 途中切れ片（「ござい」等、文として成立しない断片）"
        "\n\n【絶対に触らない／変えないもの】"
        "\n- 決定・事実・数値・固有名詞・論点・理由・立場・アクション"
        "\n- 意味やニュアンスが変わる箇所（迷ったら残す）"
        "\n- `[要確認]` タグ付き語句（文字列ごとそのまま残す）"
        "\n- 未フラグの語の推測修正（例: 「義理をか」→「桐生」等は禁止）"
        "\n\n【編集方針】"
        "\n- 実質発話は言い換えない。接着剤的な無内容部分だけ削る/整える"
        "\n- 段落・改行の流れは維持（過度な要約や箇条書き化はしない）"
        "\n- 行頭の `### ▼` / `▼` 見出し行が入力にあれば、その行は一切変更せずそのまま出力"
        "\n- `⟦FLAG0⟧` のようなプレースホルダは絶対に変更・削除・分割しない（そのまま出力）"
        "\n\n【出力形式】"
        "\n- 整えた発言録本文のみ。前置き・説明・コードフェンスは禁止"
    )
    return cached_system(static_prompt, profile_block)


def _is_heading_line(line: str) -> bool:
    return bool(_HEADING_LINE_RE.match(line.strip()))


def split_for_readable_edit(text: str, target_chars: int = READABLE_CHUNK_TARGET_CHARS) -> list[tuple[str, str]]:
    """Split into ('heading', line) or ('body', chunk) segments."""
    lines = text.splitlines()
    segments: list[tuple[str, str]] = []
    body_lines: list[str] = []
    body_len = 0

    def flush_body() -> None:
        nonlocal body_lines, body_len
        if not body_lines:
            return
        segments.append(("body", "\n".join(body_lines).strip()))
        body_lines = []
        body_len = 0

    for line in lines:
        if _is_heading_line(line):
            flush_body()
            segments.append(("heading", line.rstrip()))
            continue
        body_lines.append(line)
        body_len += len(line) + 1
        if body_len >= target_chars and not line.strip():
            flush_body()
    flush_body()
    return segments


def _split_long_body(body: str, target_chars: int = READABLE_CHUNK_TARGET_CHARS) -> list[str]:
    paragraphs = [p.strip() for p in _PARAGRAPH_SEP.split(body) if p.strip()]
    if not paragraphs:
        return [body] if body.strip() else []
    chunks: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for para in paragraphs:
        current.append(para)
        current_len += len(para)
        if current_len >= target_chars:
            chunks.append(current)
            current = []
            current_len = 0
    if current:
        if chunks and current_len < READABLE_CHUNK_MIN_CHARS // 2:
            chunks[-1].extend(current)
        else:
            chunks.append(current)
    return ["\n\n".join(part) for part in chunks]


def _expand_body_segments(segments: list[tuple[str, str]]) -> list[tuple[str, str]]:
    expanded: list[tuple[str, str]] = []
    for kind, payload in segments:
        if kind == "heading":
            expanded.append((kind, payload))
            continue
        if len(payload) <= READABLE_CHUNK_TARGET_CHARS * 1.2:
            expanded.append((kind, payload))
            continue
        for chunk in _split_long_body(payload):
            expanded.append(("body", chunk))
    return expanded


def _strip_code_fence(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _validate_chunk_output(original: str, edited: str) -> bool:
    edited = edited.strip()
    original = original.strip()
    if not edited:
        return False
    if len(edited) < len(original) * READABLE_MIN_OUTPUT_RATIO:
        return False
    if edited.count("[要確認]") < original.count("[要確認]"):
        return False
    for token in _extract_flagged_tokens(original):
        if token in edited:
            continue
        core = token.split("。")[-1]
        if core not in edited:
            return False
    return True


def _edit_one_chunk(
    client: anthropic.Anthropic,
    chunk_text: str,
    system_prompt: str | list,
) -> str:
    shielded, mapping = _shield_flagged_tokens(chunk_text)
    resp = client.messages.create(
        model=READABLE_MODEL,
        max_tokens=READABLE_MAX_TOKENS,
        temperature=0,
        timeout=READABLE_TIMEOUT_SEC,
        system=system_prompt,
        messages=[{"role": "user", "content": shielded}],
    )
    parts: list[str] = []
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", "") == "text":
            parts.append(str(getattr(block, "text", "") or ""))
    edited = _strip_code_fence("\n".join(p for p in parts if p))
    for key in mapping:
        if key not in edited:
            raise ValueError(f"placeholder_missing={key}")
    return _unshield_flagged_tokens(edited, mapping)


def polish_transcript_text(
    text: str,
    meeting_profile: dict[str, Any] | None = None,
) -> str:
    """Return readable transcript text. Falls back to input on failure."""
    source = text.strip()
    if not source:
        return text

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("readable_transcript_skipped=no_api_key")
        return text

    segments = _expand_body_segments(split_for_readable_edit(source))
    body_indices = [i for i, (kind, _) in enumerate(segments) if kind == "body"]
    if not body_indices:
        return text.strip() + "\n"

    client = anthropic.Anthropic(api_key=api_key)
    system_prompt = _build_system_prompt(meeting_profile)
    edited_bodies: dict[int, str] = {}

    def _process(idx: int) -> tuple[int, str]:
        _, chunk = segments[idx]
        try:
            edited = _edit_one_chunk(client, chunk, system_prompt)
        except Exception as e:
            print(f"readable_chunk_failed idx={idx} error={e!r}")
            return idx, chunk
        if not _validate_chunk_output(chunk, edited):
            print(f"readable_chunk_validation_failed idx={idx} fallback=original")
            return idx, chunk
        return idx, edited.strip()

    with concurrent.futures.ThreadPoolExecutor(max_workers=READABLE_MAX_PARALLEL) as executor:
        futures = [executor.submit(_process, idx) for idx in body_indices]
        for future in concurrent.futures.as_completed(futures):
            idx, body = future.result()
            edited_bodies[idx] = body

    parts: list[str] = []
    for i, (kind, payload) in enumerate(segments):
        if kind == "heading":
            parts.append(payload)
        else:
            parts.append(edited_bodies.get(i, payload))
    result = "\n\n".join(p for p in parts if p.strip())
    print(
        "readable_transcript_applied="
        f'{{"input_chars":{len(source)},"output_chars":{len(result)},"segments":{len(body_indices)}}}'
    )
    return result.strip() + "\n"


def generate_readable_transcript(
    *,
    job_dir: str,
    source_text: str,
    meeting_profile: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Write readable transcript file; return (text, output_path)."""
    polished = polish_transcript_text(source_text, meeting_profile)
    out_path = readable_transcript_path(job_dir)
    os.makedirs(job_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(polished)
    return polished, out_path


def resolve_minutes_transcript_text(
    *,
    job_dir: str,
    source_text: str,
    source_path: str,
    meeting_profile: dict[str, Any] | None = None,
) -> tuple[str, str, bool]:
    """Return (transcript_text, source_label, readable_used)."""
    if not is_readable_transcript_enabled():
        return source_text, source_path, False
    readable_text, out_path = generate_readable_transcript(
        job_dir=job_dir,
        source_text=source_text,
        meeting_profile=meeting_profile,
    )
    return readable_text, out_path, True
