"""Apply all answers from rakuten_integrated_questions.md to the working transcript.

Working file:  scripts/fixtures/job_20260701_053826_ai_with_notes.txt
Backup:        same dir, *_backup_pre_apply_<timestamp>.txt

Type A — confirmed word substitutions (pinpoint, no LLM)
  applied as direct string operations on the current text.

Type B — reader pass reconstruction (rule-enforced, see reconstruct_span.py)
  [補足:] annotations are confirmed. For spans that need real rewording
  (A-1, A-4) we call reconstruct_span(), which fixes the span boundary in
  code and only splices in the LLM's replacement after length/fact/semantic
  guards pass — the model never gets to decide how much text it touches.
  Spans that are already clear (A-2, A-3, A-5) just get their annotation
  stripped. Any guard failure falls back to strip-only (keep original text
  + drop the annotation) rather than risking an unbounded rewrite.
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
_BACKUP = _FIXTURES / "job_20260701_053826_ai_with_notes_backup_pre_apply_20260704_121811.txt"

sys.path.insert(0, str(_ROOT))
from reconstruct_span import apply_span_reconstruction, reconstruct_span  # noqa: E402

_PROTECTED_NAMES = ["合瀬さん", "鷹股さん", "山屋さん", "中本絵麻", "相原さん", "戦略推進室"]

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

# A-2/A-3/A-5: the sentence is already clear on its own; the reader-pass
# answer only confirms it, so we just drop the annotation. No LLM involved.
_STRIP_ONLY_ITEMS: list[tuple[str, str]] = [
    ("起こっちゃってるってことなんですか?", "A-2 プレイヤーレベル話（既に明確）"),
    ("やっぱり1/3ぐらい。", "A-3 1/3という数値（確定のみ）"),
    ("頭結構そんな問題なさそうで", "A-5 頭(上流)の定義（既に明確）"),
]

# A-1/A-4: the span itself is garbled and needs a real rewrite. `span_with_tag`
# is the exact literal substring (annotation included) that gets replaced —
# fixed by string match, not by an LLM-picked boundary. context_before/after
# are reference-only text shown to the model so it understands what precedes
# and follows, but they are never part of what gets spliced back in.
_RECONSTRUCT_ITEMS: list[dict[str, str]] = [
    {
        "id": "A-1 記号を取ってる(参加形式)",
        "span_with_tag": (
            "すいません、 [補足: 「参加は任意の形式で良い」と言っています。]"
            "記号を取ってる——いや、でもいいんじゃないかなと。"
        ),
        "context_before": "あ、何形式ですか?",
        "context_after": "なるほど、なるほど、そういうことですね。",
        "confirmed_info": "「参加は任意の形式で良い」と言っています。",
    },
    {
        "id": "A-4 最初やってみてもいい(サーベイ主体)",
        "span_with_tag": (
            "例えばあの僕がそうですね、最初やってみてもいいかもしれないです。 "
            "[補足: サーベイは人間が行います。AIチャットインタビューの話は別の話で、"
            "楽天インサイトのサービスの説明です。]"
        ),
        "context_before": "そういう少しヒアリングをするみたいなことします。",
        "context_after": "いきなりやる上でもなんかすごいプラスになりそうな。",
        "confirmed_info": (
            "サーベイは人間が行います。AIチャットインタビューの話は別の話で、"
            "楽天インサイトのサービスの説明です。"
        ),
    },
]


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

    # ── Type B: strip-only reader-pass items (A-2, A-3, A-5) ───────────────
    for anchor, desc in _STRIP_ONLY_ITEMS:
        anchor_pos = out.find(anchor)
        if anchor_pos < 0:
            report.append({"id": desc, "action": "anchor_not_found"})
            continue
        tag_m = None
        for m in _SUPPLEMENT_RE.finditer(out, max(0, anchor_pos - 50)):
            if m.start() > anchor_pos + 200:
                break
            tag_m = m
            break
        if tag_m is None:
            report.append({"id": desc, "action": "tag_not_found"})
            continue
        out = out[: tag_m.start()] + out[tag_m.end():]
        report.append({"id": desc, "action": "strip_annotation", "stripped": tag_m.group()})

    # ── Type B: rule-enforced reconstruction (A-1, A-4) ────────────────────
    for item in _RECONSTRUCT_ITEMS:
        span_with_tag = item["span_with_tag"]
        pos = out.find(span_with_tag)
        if pos < 0:
            report.append({"id": item["id"], "action": "span_not_found"})
            continue
        start, end = pos, pos + len(span_with_tag)
        clean_target = _SUPPLEMENT_RE.sub("", span_with_tag).strip()

        print(f"  [reconstruct_span] {item['id']}...")
        result = reconstruct_span(
            span_target=clean_target,
            context_before=item["context_before"],
            context_after=item["context_after"],
            confirmed_info=item["confirmed_info"],
            protected_names=_PROTECTED_NAMES,
        )

        if result.ok:
            out = out[:start] + result.replacement + out[end:]
            report.append({
                "id": item["id"],
                "action": "reconstructed",
                "before": clean_target,
                "after": result.replacement,
            })
        else:
            # Guard failed (length/fact/semantic) or no API key → fall back
            # to the original text with its [補足:] annotation intact (not
            # stripped), so the unresolved span stays visibly flagged rather
            # than silently reverting to bare garbled text.
            report.append({
                "id": item["id"],
                "action": "reconstruct_rejected_fallback_keep_annotation",
                "reason": result.reason,
                "stage": result.stage,
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

    if not _BACKUP.exists():
        print(f"ERROR: clean backup not found: {_BACKUP}", file=sys.stderr)
        sys.exit(1)

    # Always start from the pre-apply backup (TypeA only, no TypeB) so a
    # half-applied previous run never carries forward.
    text = _BACKUP.read_text(encoding="utf-8")
    print(f"Loaded from backup: {_BACKUP.name}  ({len(text):,} chars)")
    print(f"[補足:] count before: {text.count('[補足:')}")

    # Snapshot whatever is currently on disk before overwriting it.
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if _TRANSCRIPT.exists():
        pre_run_backup = _TRANSCRIPT.parent / (
            _TRANSCRIPT.stem + f"_pre_run_{ts}" + _TRANSCRIPT.suffix
        )
        pre_run_backup.write_text(_TRANSCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"Pre-run snapshot: {pre_run_backup.name}")

    # Apply
    print("\n── Type A + Type B ──")
    corrected, report = apply(text)

    # Report
    type_a_applied = 0
    type_b_applied = 0
    for item in report:
        action = item.get("action", "")
        item_id = item.get("id", "")
        if item_id.startswith("A-"):
            type_b_applied += 1
            icon = "✓" if "reconstruct" in action or "strip" in action else "✗"
        else:
            type_a_applied += 1 if "replace" in action or "delete" in action else 0
            icon = "✓" if "replace" in action or "delete" in action else "·"
        print(f"  {icon} {item_id}: {action}", end="")
        if "count" in item:
            print(f" ×{item['count']}", end="")
        if "from" in item:
            print(f" ({item['from']} → {item['to']})", end="")
        if "phrase" in item and "delete" in action:
            print(f" [{item['phrase'][:40]}]", end="")
        if "stage" in item:
            print(f" (stage={item['stage']}, reason={item.get('reason', '')})", end="")
        print()

    print(f"\n[補足:] count after:  {corrected.count('[補足:')}")
    print(f"Chars: {len(text):,} → {len(corrected):,} (Δ{len(corrected)-len(text):+})")

    # Write
    _TRANSCRIPT.write_text(corrected, encoding="utf-8")
    print(f"\nWritten: {_TRANSCRIPT.name}")

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
