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
READABLE_MODEL = "claude-sonnet-5"
READABLE_MAX_TOKENS = 8192
READABLE_TIMEOUT_SEC = 180
READABLE_CHUNK_TARGET_CHARS = 3500
READABLE_CHUNK_MIN_CHARS = 800
READABLE_SPLIT_RETRY_TARGET_CHARS = 800
READABLE_MAX_PARALLEL = 4
READABLE_MIN_OUTPUT_RATIO = 0.25
# 検証失敗チャンクの再試行温度（0 の再試行は決定論的で無意味なため少し上げる）
READABLE_RETRY_TEMPERATURE = 0.4

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
        "\n\n【相槌の織り込み解消（重要）】"
        "\n- 話し手の一文の中に聞き手の相槌（はい・うん・ええ・なるほど・そうですね 等）が"
        "縫い込まれている場合、相槌だけを取り除き話し手の文を自然につなげる。"
        "\n  例: 『はいで、L2が5クラスで109名』→『で、L2が5クラスで109名』"
        "\n  例: 『実施をしない方向、はいに考えています』→『実施をしない方向に考えています』"
        "\n- ただし相槌の除去で話し手の語順・語彙は変えないこと（接合のみ）。"
        "\n- 質問への回答としての『はい』『いいえ』（同意・返答の実質を持つもの）は残す。"
        "\n\n【文脈から修正してよい誤認識】"
        "\n- 前後の説明だけで正解が一意に決まる一般語・助詞・単位の誤変換は修正する。"
        "\n  例: KPIツリーが200〜300個の行で構成される文脈の『200秒』→『200行』、"
        "薬効の文脈の『聞く薬』→『効く薬』、役割分担の『3位いっぱい』→『三位一体』。"
        "\n- 同じ文書内に正しい表記が反復されている場合は、その表記に統一する。"
        "\n- 文脈上意味を成さない断片は、直後の明確な言い直しと同義なら削除・接合してよい。"
        "\n- 正解が複数あり得る固有名詞・数値・事実は推測修正しない。"
        "\n\n【絶対に触らない／変えないもの】"
        "\n- 決定・事実・数値・固有名詞・論点・理由・立場・アクション"
        "\n- 意味やニュアンスが変わる箇所（迷ったら残す）"
        "\n- `[要確認]` タグ付き語句（文字列ごとそのまま残す）"
        "\n- `[補足: ...]` アノテーション（reader pass が挿入した補足注釈。文字列ごとそのまま残す）"
        "\n- 文脈だけでは一意に確定できない未フラグ語の推測修正"
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
    units: list[str] = []
    for para in paragraphs:
        if len(para) <= target_chars:
            units.append(para)
            continue
        sentences = [
            s.strip()
            for s in re.split(r"(?<=[。！？!?])", para)
            if s.strip()
        ]
        if len(sentences) <= 1:
            # Last-resort hard split for ASR output without punctuation.
            units.extend(
                para[i : i + target_chars]
                for i in range(0, len(para), target_chars)
            )
        else:
            sentence_group: list[str] = []
            sentence_len = 0
            for sentence in sentences:
                if sentence_group and sentence_len + len(sentence) > target_chars:
                    units.append("".join(sentence_group))
                    sentence_group = []
                    sentence_len = 0
                sentence_group.append(sentence)
                sentence_len += len(sentence)
            if sentence_group:
                units.append("".join(sentence_group))
    chunks: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for unit in units:
        if current and current_len + len(unit) > target_chars:
            chunks.append(current)
            current = []
            current_len = 0
        current.append(unit)
        current_len += len(unit)
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


def _validate_chunk_output(
    original: str,
    edited: str,
    meeting_profile: dict[str, Any] | None = None,
) -> bool:
    edited = edited.strip()
    original = original.strip()
    if not edited:
        return False
    if len(edited) < len(original) * READABLE_MIN_OUTPUT_RATIO:
        return False
    if edited.count("[要確認]") < original.count("[要確認]"):
        return False
    if edited.count("[要確認]") > original.count("[要確認]"):
        return False
    for token in _extract_flagged_tokens(original):
        if token in edited:
            continue
        core = token.split("。")[-1]
        if core not in edited:
            return False
    try:
        from fact_integrity_gate import verify_fact_integrity

        if not verify_fact_integrity(
            original,
            edited,
            meeting_profile=meeting_profile,
        ).ok:
            return False
    except Exception as e:  # noqa: BLE001
        print(f"readable_fact_validation_failed error={e!r}")
        return False
    return True


def _edit_one_chunk(
    client: anthropic.Anthropic,
    chunk_text: str,
    system_prompt: str | list,
    *,
    temperature: float = 0,
) -> str:
    shielded, mapping = _shield_flagged_tokens(chunk_text)
    request: dict[str, Any] = {
        "model": READABLE_MODEL,
        "max_tokens": READABLE_MAX_TOKENS,
        "timeout": READABLE_TIMEOUT_SEC,
        "system": system_prompt,
        "messages": [{"role": "user", "content": shielded}],
    }
    # Sonnet 5 rejects the legacy sampling parameter with HTTP 400.
    # Keep it only for older snapshots used by an explicit override/test.
    if not READABLE_MODEL.startswith("claude-sonnet-5"):
        request["temperature"] = temperature
    resp = client.messages.create(
        **request,
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


def polish_transcript_text_with_stats(
    text: str,
    meeting_profile: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return (readable text, stats). Falls back to input on failure.

    stats = {"total_chunks": int, "failed_chunk_idx": [int, ...], "retried_ok": int}
    failed_chunk_idx はリトライ後も検証に失敗し生テキストを採用したチャンク。
    """
    stats: dict[str, Any] = {
        "total_chunks": 0,
        "failed_chunk_idx": [],
        "retried_ok": 0,
        "split_recovered": 0,
    }
    source = text.strip()
    if not source:
        return text, stats

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("readable_transcript_skipped=no_api_key")
        return text, stats

    segments = _expand_body_segments(split_for_readable_edit(source))
    body_indices = [i for i, (kind, _) in enumerate(segments) if kind == "body"]
    stats["total_chunks"] = len(body_indices)
    if not body_indices:
        return text.strip() + "\n", stats

    client = anthropic.Anthropic(api_key=api_key)
    system_prompt = _build_system_prompt(meeting_profile)
    edited_bodies: dict[int, str] = {}

    def _attempt(idx: int, chunk: str, *, temperature: float) -> str | None:
        """Return edited text or None on failure."""
        try:
            edited = _edit_one_chunk(
                client, chunk, system_prompt, temperature=temperature
            )
        except Exception as e:
            print(f"readable_chunk_failed idx={idx} error={e!r}")
            return None
        if not _validate_chunk_output(chunk, edited, meeting_profile):
            print(f"readable_chunk_validation_failed idx={idx}")
            return None
        return edited.strip()

    def _process(idx: int) -> tuple[int, str, bool, bool, bool]:
        """Return (idx, body, failed, retried_ok, split_recovered)."""
        _, chunk = segments[idx]
        edited = _attempt(idx, chunk, temperature=0)
        if edited is not None:
            return idx, edited, False, False, False
        # 1回だけリトライ。temperature=0 の再試行は決定論的で同じ検証失敗に
        # 陥るため、わずかに温度を上げてサンプリングを変え、検証を通る別解を狙う。
        print(f"readable_chunk_retry idx={idx} temperature={READABLE_RETRY_TEMPERATURE}")
        edited = _attempt(idx, chunk, temperature=READABLE_RETRY_TEMPERATURE)
        if edited is not None:
            return idx, edited, False, True, False
        subchunks = _split_long_body(
            chunk,
            target_chars=READABLE_SPLIT_RETRY_TARGET_CHARS,
        )
        if len(subchunks) > 1:
            recovered: list[str] = []
            for subchunk in subchunks:
                subedited = _attempt(
                    idx,
                    subchunk,
                    temperature=READABLE_RETRY_TEMPERATURE,
                )
                if subedited is None:
                    recovered = []
                    break
                recovered.append(subedited)
            if recovered:
                return idx, "\n\n".join(recovered), False, False, True
        print(f"readable_chunk_retry_failed idx={idx} fallback=original")
        return idx, chunk, True, False, False

    with concurrent.futures.ThreadPoolExecutor(max_workers=READABLE_MAX_PARALLEL) as executor:
        futures = [executor.submit(_process, idx) for idx in body_indices]
        for future in concurrent.futures.as_completed(futures):
            idx, body, failed, retried_ok, split_recovered = future.result()
            edited_bodies[idx] = body
            if failed:
                stats["failed_chunk_idx"].append(idx)
            if retried_ok:
                stats["retried_ok"] += 1
            if split_recovered:
                stats["split_recovered"] += 1
    stats["failed_chunk_idx"].sort()

    parts: list[str] = []
    for i, (kind, payload) in enumerate(segments):
        if kind == "heading":
            parts.append(payload)
        else:
            parts.append(edited_bodies.get(i, payload))
    result = "\n\n".join(p for p in parts if p.strip())
    print(
        "readable_transcript_applied="
        f'{{"input_chars":{len(source)},"output_chars":{len(result)},'
        f'"segments":{len(body_indices)},'
        f'"failed_chunks":{len(stats["failed_chunk_idx"])},'
        f'"retried_ok":{stats["retried_ok"]},'
        f'"split_recovered":{stats["split_recovered"]}}}'
    )
    return result.strip() + "\n", stats


def polish_transcript_text(
    text: str,
    meeting_profile: dict[str, Any] | None = None,
) -> str:
    """Return readable transcript text. Falls back to input on failure."""
    polished, _ = polish_transcript_text_with_stats(text, meeting_profile)
    return polished


def generate_readable_transcript(
    *,
    job_dir: str,
    source_text: str,
    meeting_profile: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Write readable transcript file; return (text, output_path)."""
    polished, _, out_path = generate_readable_transcript_with_stats(
        job_dir=job_dir,
        source_text=source_text,
        meeting_profile=meeting_profile,
    )
    return polished, out_path


def generate_readable_transcript_with_stats(
    *,
    job_dir: str,
    source_text: str,
    meeting_profile: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any], str]:
    """Write readable transcript file; return (text, stats, output_path)."""
    polished, stats = polish_transcript_text_with_stats(source_text, meeting_profile)

    # 最終批評パス（単一目的の全文レビュー）。off/shadow/apply は env で制御。
    # 失敗しても polished をそのまま使う（非致命）。
    try:
        from final_review_pass import resolve_final_review_mode, run_final_review

        if resolve_final_review_mode() != "off":
            polished, review_report = run_final_review(
                job_dir=job_dir, text=polished
            )
            stats["final_review"] = review_report
    except Exception as e:  # noqa: BLE001
        print(f"final_review_pass_skipped={e!r}")

    out_path = readable_transcript_path(job_dir)
    os.makedirs(job_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(polished)
    return polished, stats, out_path


def resolve_minutes_transcript_text(
    *,
    job_dir: str,
    source_text: str,
    source_path: str,
    meeting_profile: dict[str, Any] | None = None,
) -> tuple[str, str, bool]:
    """Return (transcript_text, source_label, readable_used)."""
    text, source_label, readable_used, _ = resolve_minutes_transcript_text_with_stats(
        job_dir=job_dir,
        source_text=source_text,
        source_path=source_path,
        meeting_profile=meeting_profile,
    )
    return text, source_label, readable_used


def resolve_minutes_transcript_text_with_stats(
    *,
    job_dir: str,
    source_text: str,
    source_path: str,
    meeting_profile: dict[str, Any] | None = None,
) -> tuple[str, str, bool, dict[str, Any]]:
    """Return (transcript_text, source_label, readable_used, stats)."""
    empty_stats: dict[str, Any] = {
        "total_chunks": 0,
        "failed_chunk_idx": [],
        "retried_ok": 0,
        "split_recovered": 0,
    }
    if not is_readable_transcript_enabled():
        return source_text, source_path, False, empty_stats
    readable_text, stats, out_path = generate_readable_transcript_with_stats(
        job_dir=job_dir,
        source_text=source_text,
        meeting_profile=meeting_profile,
    )
    return readable_text, out_path, True, stats
