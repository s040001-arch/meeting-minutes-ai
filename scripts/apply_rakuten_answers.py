"""Apply all answers from rakuten_integrated_questions.md to the working transcript.

Working file:  scripts/fixtures/job_20260701_053826_ai_with_notes.txt
Backup:        same dir, *_backup_pre_apply_<timestamp>.txt

Type A — confirmed word substitutions (pinpoint, no LLM)
  applied as direct string operations on the current text.

Type B — reader pass reconstruction (Opus 4.8)
  [補足:] annotations are confirmed; attempt Opus reconstruction of the
  ±1 sentence span; verify numbers/names preserved; fall back to
  annotation-strip-only if reconstruction fails or API unavailable.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from pathlib import Path

# ── paths ──────────────────────────────────────────────────────────────────

_ROOT = Path(__file__).parent.parent
_FIXTURES = _ROOT / "scripts" / "fixtures"
_TRANSCRIPT = _FIXTURES / "job_20260701_053826_ai_with_notes.txt"

# ── Type A: word-level substitutions ──────────────────────────────────────

# Global replaces (all remaining occurrences in text):
_GLOBAL_SUBS: list[tuple[str, str, str]] = [
    ("大瀬さん",   "合瀬さん",    "B-1 合瀬グループ"),
    ("人脈推進室", "戦略推進室",  "B-5 推進室名"),
    ("中本エマ",   "中本絵麻",    "B-6 中本さん名前"),
    ("制度高い",   "精度が高い",  "B-19 精度表記"),
]

# Context-bound replace (only when preceded by the given pattern):
_CONTEXT_SUBS: list[tuple[str, str, str, str]] = [
    # (context_pattern_before, old, new, desc)
    (r"部長が", "坂本さん", "鷹股さん", "B-7 坂本→鷹股(部長コンテキスト)"),
]

# Span deletions: delete the EXACT phrase (and optional trailing separator):
_DELETIONS: list[tuple[str, str]] = [
    # phrase to delete, description
    (
        "まあのAIを使った正しい部分が例えばね。はいみたいなのが、",
        "B-26 正しい部分(filler_garble)",
    ),
    (
        "今さ行って国民で前編はシステム開発から向かなくないは",
        "B-25 国民で前編(lexical)",
    ),
]

# Skipped proposals (already corrected in current text):
# さんが最初, ヤさん, 風さん, 高松さん, 高尾山, 高又さん, こうかつ, 山谷さん,
# 夜中, オリジバリア, でゅんさん, せさん, 音声さん, あっさん, 厚さん,
# セカンドさん, バイオリン形成, ブランドレッドサーメ, ブランドサーベ,
# 頼んだの部門, グラビ調査, 開発詐欺, PD勝負, 歌詞でも下手, U デミ,
# 仲間さん, アさん  (aw_count=0 in current text)

# ── Type B: reader pass annotations ────────────────────────────────────────

_SUPPLEMENT_RE = re.compile(r" \[補足: ([^\]]+)\]")

# Confirmed answers for each [補足:] annotation (in order of appearance):
_READER_PASS_ITEMS = [
    # (unique_anchor_substring, confirmed_info, attempt_reconstruction)
    (
        "記号を取ってる",
        "「参加は任意の形式で良い」と言っています。",
        True,   # A-1: garbled text → attempt Opus reconstruction
    ),
    (
        "起こっちゃってるってことなんですか?",
        "ここは調べないとわからないという回答。その後のサーベイの話に繋がる。",
        False,  # A-2: text is clear, just strip annotation
    ),
    (
        "やっぱり1/3ぐらい。",
        "合瀬さん部門の承認通過率が1/3。",
        False,  # A-3: number confirmed, just strip annotation
    ),
    (
        "最初やってみてもいいかもしれないです。",
        "サーベイは人間が行います。AIチャットインタビューは別の話（楽天インサイトのサービス）。",
        True,   # A-4: context clarification → attempt Opus reconstruction
    ),
    (
        "頭結構そんな問題なさそうで",
        "これは例。上流から順にチェックする話。",
        False,  # A-5: context is already clear, just strip annotation
    ),
]

# ── helpers ────────────────────────────────────────────────────────────────

def _sentence_boundaries(text: str, pos: int) -> tuple[int, int]:
    """Return start/end of the ±1 sentence window around pos."""
    # sentence end markers
    ends = "\n。？！"
    # find start: go back to last sentence boundary
    start = pos
    while start > 0 and text[start - 1] not in ends:
        start -= 1
    # extend one more sentence back
    if start > 0:
        prev = start - 1
        while prev > 0 and text[prev - 1] not in ends:
            prev -= 1
        start = prev
    # find end: go forward to next two sentence boundaries
    end = pos
    count = 0
    while end < len(text) and count < 2:
        if text[end] in ends:
            count += 1
        end += 1
    return max(0, start), min(len(text), end)


def _numbers_preserved(orig: str, rewritten: str) -> bool:
    """Check that all numeric tokens in orig still appear in rewritten."""
    nums = re.findall(r"\d+(?:[./]\d+)?", orig)
    for n in nums:
        if n not in rewritten:
            return False
    return True


def _opus_reconstruct(span: str, confirmed_info: str) -> str | None:
    """Call Claude Opus to reconstruct a span. Returns text or None on failure."""
    try:
        import anthropic  # type: ignore
    except ImportError:
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        # Try .env
        env_path = _ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if line.startswith("ANTHROPIC_API_KEY="):
                    api_key = line.split("=", 1)[1].strip().strip('"\'')
                    break
    if not api_key:
        return None

    client = anthropic.Anthropic(api_key=api_key)
    prompt = (
        "以下は日本語の会議逐語録の一部です。\n\n"
        f"【テキスト】\n{span}\n\n"
        f"【確定情報】\n{confirmed_info}\n\n"
        "指示:\n"
        "- テキスト中の曖昧または崩れた表現のみ、確定情報を参考に最小限修正してください。\n"
        "- 意味が既に明確な箇所はそのままにしてください。\n"
        "- 数値・固有名詞・事実は一切変えないでください。\n"
        "- 修正テキストのみを出力してください（説明不要）。"
    )
    try:
        resp = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=1024,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        result = ""
        for block in (resp.content or []):
            if getattr(block, "type", "") == "text":
                result += block.text
        return result.strip() or None
    except Exception as e:
        print(f"  [Opus API error] {e}")
        return None


# ── main apply function ────────────────────────────────────────────────────

def apply(text: str) -> tuple[str, list[dict]]:
    """Apply all corrections. Returns (corrected_text, report_items)."""
    report: list[dict] = []
    out = text

    # ── Type A: global word subs ──────────────────────────────────────────
    for old, new, desc in _GLOBAL_SUBS:
        count = out.count(old)
        if count == 0:
            report.append({"id": desc, "action": "skip_already_applied", "count": 0})
            continue
        out = out.replace(old, new)
        report.append({"id": desc, "action": "replace", "from": old, "to": new, "count": count})

    # ── Type A: context-bound subs ────────────────────────────────────────
    for ctx_pattern, old, new, desc in _CONTEXT_SUBS:
        pattern = ctx_pattern + re.escape(old)
        matches = list(re.finditer(pattern, out))
        if not matches:
            report.append({"id": desc, "action": "skip_not_found"})
            continue
        for m in matches:
            full_match = m.group()
            replacement = full_match.replace(old, new, 1)
            out = out[:m.start()] + replacement + out[m.end():]
        report.append({
            "id": desc, "action": "replace", "from": old, "to": new, "count": len(matches)
        })

    # ── Type A: span deletions ────────────────────────────────────────────
    for phrase, desc in _DELETIONS:
        if phrase not in out:
            # Try without trailing punctuation / whitespace variants
            report.append({"id": desc, "action": "skip_not_found", "phrase": phrase})
            continue
        before_ctx = out[max(0, out.find(phrase) - 40) : out.find(phrase) + 40]
        out = out.replace(phrase, "", 1)
        report.append({"id": desc, "action": "delete", "phrase": phrase, "ctx": before_ctx})

    # ── Type B: reader pass ────────────────────────────────────────────────
    for anchor, confirmed_info, try_reconstruct in _READER_PASS_ITEMS:
        # Find the [補足:] tag near this anchor
        anchor_pos = out.find(anchor)
        if anchor_pos < 0:
            report.append({"id": f"reader_pass({anchor[:20]})", "action": "anchor_not_found"})
            continue

        # Find the [補足:] annotation near anchor_pos
        tag_m = None
        search_start = max(0, anchor_pos - 10)
        search_end = min(len(out), anchor_pos + len(anchor) + 5)
        # Look for [補足: ...] immediately before or after anchor
        for m in _SUPPLEMENT_RE.finditer(out, max(0, anchor_pos - 50)):
            if m.start() > anchor_pos + 200:
                break
            tag_m = m
            break

        if tag_m is None:
            report.append({"id": f"reader_pass({anchor[:20]})", "action": "tag_not_found"})
            continue

        tag_start = tag_m.start()
        tag_end = tag_m.end()
        tag_text = tag_m.group()
        tag_inner = tag_m.group(1)

        if not try_reconstruct:
            # Simply strip the [補足:] annotation
            out = out[:tag_start] + out[tag_end:]
            report.append({
                "id": f"reader_pass({anchor[:20]})",
                "action": "strip_annotation",
                "stripped": tag_text,
            })
            continue

        # Attempt Opus reconstruction on ±1 sentence span
        span_start, span_end = _sentence_boundaries(out, tag_start)
        span_orig = out[span_start:span_end]
        # Remove annotation from span for Opus input
        span_clean = _SUPPLEMENT_RE.sub("", span_orig).strip()

        print(f"  [Opus] Reconstructing span for anchor '{anchor[:30]}'...")
        print(f"    span_clean: {repr(span_clean[:120])}")
        reconstructed = _opus_reconstruct(span_clean, confirmed_info)

        if reconstructed is None:
            # API unavailable → strip annotation only
            out = out[:tag_start] + out[tag_end:]
            report.append({
                "id": f"reader_pass({anchor[:20]})",
                "action": "strip_only_api_unavailable",
                "stripped": tag_text,
            })
            continue

        print(f"    Opus output: {repr(reconstructed[:120])}")

        # Fact check: numbers/names preserved
        if not _numbers_preserved(span_clean, reconstructed):
            print(f"    [GATE FAIL] number mismatch → strip only")
            out = out[:tag_start] + out[tag_end:]
            report.append({
                "id": f"reader_pass({anchor[:20]})",
                "action": "strip_only_gate_fail",
                "reason": "number_mismatch",
            })
            continue

        # Apply: replace original span (with annotation) with reconstructed
        out = out[:span_start] + reconstructed + out[span_end:]
        report.append({
            "id": f"reader_pass({anchor[:20]})",
            "action": "reconstructed",
            "before": span_orig[:120],
            "after": reconstructed[:120],
        })

    return out, report


# ── CLI ────────────────────────────────────────────────────────────────────

def main() -> None:
    # Load .env if exists
    env_path = _ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"\''))

    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    src = _TRANSCRIPT
    if not src.exists():
        print(f"ERROR: transcript not found: {src}", file=sys.stderr)
        sys.exit(1)

    text = src.read_text(encoding="utf-8")
    print(f"Loaded: {src.name}  ({len(text):,} chars)")
    print(f"[補足:] count before: {text.count('[補足:')}")

    # Backup
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = src.stem + f"_backup_pre_apply_{ts}" + src.suffix
    backup_path = src.parent / backup_name
    backup_path.write_text(text, encoding="utf-8")
    print(f"Backup: {backup_path.name}")

    # Apply
    print("\n── Type A: word substitutions ──")
    corrected, report = apply(text)

    # Report
    type_a_applied = 0
    type_b_applied = 0
    for item in report:
        action = item.get("action", "")
        if "reader_pass" in item.get("id", ""):
            type_b_applied += 1
            icon = "✓" if "reconstruct" in action or "strip" in action else "✗"
        else:
            type_a_applied += 1 if "replace" in action or "delete" in action else 0
            icon = "✓" if "replace" in action or "delete" in action else "·"
        print(f"  {icon} {item['id']}: {action}", end="")
        if "count" in item:
            print(f" ×{item['count']}", end="")
        if "from" in item:
            print(f" ({item['from']} → {item['to']})", end="")
        if "phrase" in item and "delete" in action:
            print(f" [{item['phrase'][:40]}]", end="")
        print()

    print(f"\n[補足:] count after:  {corrected.count('[補足:')}")
    print(f"Chars: {len(text):,} → {len(corrected):,} (Δ{len(corrected)-len(text):+})")

    # Write
    src.write_text(corrected, encoding="utf-8")
    print(f"\nWritten: {src.name}")

    # Key before/after excerpts
    print("\n── Key before/after (5 items) ──")
    _show_diff(text, corrected, "大瀬さん", "合瀬さん")
    _show_diff(text, corrected, "人脈推進室", "戦略推進室")
    _show_diff(text, corrected, "中本エマ", "中本絵麻")
    _show_diff(text, corrected, "部長が坂本さん", "部長が鷹股さん")
    _show_diff(text, corrected, "制度高い", "精度が高い")


def _show_diff(before: str, after: str, old: str, new: str) -> None:
    pos = before.find(old)
    if pos >= 0:
        ctx_b = before[max(0, pos - 20) : pos + len(old) + 30]
        print(f"  BEFORE: …{ctx_b}…")
    pos2 = after.find(new)
    if pos2 >= 0:
        ctx_a = after[max(0, pos2 - 20) : pos2 + len(new) + 30]
        print(f"  AFTER:  …{ctx_a}…")
    elif pos >= 0:
        print(f"  AFTER:  ('{old}' not found in output)")
    print()


if __name__ == "__main__":
    main()
