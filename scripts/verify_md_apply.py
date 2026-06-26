#!/usr/bin/env python3
"""Step 3 verification: MD answer auto-parse → ANSWER_ITEMS → apply → compare.

Validates that the automated pipeline (parse_md_answers + interpret_answer +
bridge) produces an after_qa identical to the hand-crafted apply script.

Steps
-----
1. Parse docs/phase10_164142_ask_review_combined.md
2. Interpret each answer via interpret_md_answer.interpret_answer
3. Build ANSWER_ITEMS via bridge logic (span_before from MD spans)
4. Compare auto-ANSWER_ITEMS with manual-ANSWER_ITEMS from
   scripts/apply_164142_ask_review_combined.py
5. Apply auto-ANSWER_ITEMS to fixture → auto_after_qa
6. Diff auto_after_qa vs existing data/164142_after_qa.txt
7. Run fact gate on auto_after_qa
"""
from __future__ import annotations

import sys
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from parse_md_answers import parse_md_answers
from interpret_md_answer import interpret_answer
from pinpoint_answer_apply import apply_answers
from fact_integrity_gate import verify_fact_integrity

# ── source files ─────────────────────────────────────────────────────────────

MD_PATH         = ROOT / "docs/phase10_164142_ask_review_combined.md"
FIXTURE         = ROOT / "data/fixtures/job_164142_step3/merged_transcript_after_qa.txt"
MANUAL_AFTER_QA = ROOT / "data/164142_after_qa.txt"

# Minimum similarity ratio to accept a fuzzy-context match.
# Below this threshold the MD span context is too different from the source
# (e.g., step-3 already fixed the anomaly but a different occurrence remains).
_FUZZY_SIM_THRESHOLD = 0.35


# ── bridge: interpret output → ANSWER_ITEMS ──────────────────────────────────

def _span_safe_for_source(span: str, anomaly_word: str, source: str) -> bool:
    """Return True if it is safe to apply this span against source.

    A span is safe when either:
    - it appears verbatim in source (exact match), OR
    - the anomaly_word is in source AND the source context around it is
      sufficiently similar to the MD span (ratio >= _FUZZY_SIM_THRESHOLD).

    Returns False when anomaly_word is absent (step-3 fully corrected) or
    when the surrounding context is too different (wrong-context risk).
    """
    if span in source:
        return True  # verbatim → always safe

    aw_idx = source.find(anomaly_word)
    if aw_idx == -1:
        return False  # anomaly not in source at all → step-3 corrected

    # Get a context window centered on the anomaly, same length as the MD span
    half = max(len(span) // 2, len(anomaly_word))
    ctx_start = max(0, aw_idx - half)
    ctx_end   = min(len(source), ctx_start + len(span))
    source_ctx = source[ctx_start:ctx_end]
    ratio = SequenceMatcher(None, span, source_ctx).ratio()
    return ratio >= _FUZZY_SIM_THRESHOLD


def _build_auto_items(parsed: list[dict], source_text: str | None = None) -> list[dict]:
    """Convert parsed MD items into ANSWER_ITEMS for apply_answers.

    When source_text is provided, spans that are neither verbatim in the
    source nor contextually similar to any occurrence of the anomaly_word
    are silently skipped (they were likely fixed by step-3 auto-correct).
    """
    items: list[dict] = []
    skipped_unsafe: list[tuple] = []

    for p in parsed:
        ri     = p["review_index"]
        aw     = p["anomaly_word"]  # from MD header
        spans  = p.get("spans", [])
        result = interpret_answer(p["answer_text"], anomaly_word=aw)
        action = result["action"]

        if action == "keep":
            continue

        if action == "error":
            raise RuntimeError(f"#{ri} {aw!r}: interpreter error: {result['message']}")

        if not spans:
            print(f"  WARN #{ri} {aw!r}: no spans in MD → skip", file=sys.stderr)
            continue

        def _safe(span: str) -> bool:
            if source_text is None:
                return True
            ok = _span_safe_for_source(span, aw, source_text)
            if not ok:
                skipped_unsafe.append((ri, aw, span))
            return ok

        if action == "replace":
            word = result["word"]
            for i, span in enumerate(spans):
                if not _safe(span):
                    continue
                items.append({
                    "review_index": f"{ri}-{i+1}" if len(spans) > 1 else ri,
                    "anomaly_word": aw,
                    "span_before":  span,
                    "answer_text":  word,
                    "hypothesis":   "",
                })

        elif action == "delete":
            for i, span in enumerate(spans):
                if not _safe(span):
                    continue
                items.append({
                    "review_index": f"{ri}-{i+1}" if len(spans) > 1 else ri,
                    "anomaly_word": aw,
                    "span_before":  span,
                    "answer_text":  "削除",
                    "hypothesis":   "",
                })

        elif action == "delete_phrase":
            phrase = result["phrase"]
            for i, span in enumerate(spans):
                if phrase in span:
                    if not _safe(span):
                        continue
                    phrase_pos = span.find(phrase)
                    if phrase_pos == 0:
                        # phrase at START of span: anomaly=span, answer=kept_suffix
                        kept = span[len(phrase):]
                        items.append({
                            "review_index": ri,
                            "anomaly_word": span,  # whole span is the anomaly
                            "span_before":  span,
                            "answer_text":  kept,
                            "hypothesis":   "",
                        })
                    else:
                        # phrase in MIDDLE: anomaly=phrase, answer=prefix+suffix
                        corrected = span[:phrase_pos] + span[phrase_pos + len(phrase):]
                        items.append({
                            "review_index": ri,
                            "anomaly_word": phrase,
                            "span_before":  span,
                            "answer_text":  corrected,
                            "hypothesis":   "",
                        })
                else:
                    # phrase extends beyond MD span → use phrase as anomaly/span, DELETE
                    if source_text is not None and not _span_safe_for_source(phrase, phrase, source_text):
                        skipped_unsafe.append((ri, aw, phrase))
                        continue
                    items.append({
                        "review_index": ri,
                        "anomaly_word": phrase,
                        "span_before":  phrase,
                        "answer_text":  "削除",
                        "hypothesis":   "",
                    })

    if skipped_unsafe:
        print(f"  Skipped (unsafe context / step-3 corrected): {len(skipped_unsafe)}")
        for ri, aw, sp in skipped_unsafe:
            print(f"    #{ri} {aw!r} span={sp[:45]!r}")

    return items


# ── manual ANSWER_ITEMS (ground truth for comparison) ────────────────────────

MANUAL_ITEMS = [
    {"review_index": 5,    "anomaly_word": "映画",
     "span_before": "1 個の映画ですね",
     "answer_text": "シナリオ",           "hypothesis": ""},
    {"review_index": 6,    "anomaly_word": "5 月に行きやつ",
     "span_before": "5 月に行きやつ行きますよね",
     "answer_text": "5 月に",             "hypothesis": ""},
    {"review_index": "8a", "anomaly_word": "5万円",
     "span_before": "5万円そうすよね。半日ですよね",
     "answer_text": "52.5万円",           "hypothesis": "25万円"},
    {"review_index": 15,   "anomaly_word": "FP 事業の中で真面目に",
     "span_before": "FP 事業の中で真面目に",
     "answer_text": "真面目に",           "hypothesis": ""},
    {"review_index": 18,   "anomaly_word": "鋭角",
     "span_before": "豊通でまた鋭角と言われている",
     "answer_text": "A格",               "hypothesis": ""},
    {"review_index": 21,   "anomaly_word": "小松放棄",
     "span_before": "小松放棄もやりましたね",
     "answer_text": "小松鋼機",           "hypothesis": ""},
    {"review_index": 22,   "anomaly_word": "松コープ",
     "span_before": "松コープと矢崎",
     "answer_text": "小松鋼機",           "hypothesis": ""},
    {"review_index": 23,   "anomaly_word": "ビジネスがワンマン不協さん、",
     "span_before": "秋本さんビジネスがワンマン不協さん、あの開発",
     "answer_text": "秋本さんあの開発",   "hypothesis": ""},
    {"review_index": 30,   "anomaly_word": "ベース 3",
     "span_before": "根本さんがベース 3 を作ってくれてた",
     "answer_text": "ベース",             "hypothesis": ""},
    {"review_index": 36,   "anomaly_word": "儲かりのフェア",
     "span_before": "切り口で儲かりのフェアをやりまなぞ。なぞ対策を議論していく",
     "answer_text": "Where",             "hypothesis": ""},
    {"review_index": 37,   "anomaly_word": "予兆会社",
     "span_before": "予兆会社そうなんすよ",
     "answer_text": "削除",               "hypothesis": ""},
    {"review_index": 38,   "anomaly_word": "者向け見解者向けてはい下だしからのえ、",
     "span_before": "者向け見解者向けてはい下だしからのえ、",
     "answer_text": "削除",               "hypothesis": ""},
    {"review_index": 39,   "anomaly_word": "バイかけたい",
     "span_before": "盆前にバイかけたい",
     "answer_text": "かけたい",           "hypothesis": ""},
    {"review_index": 47,   "anomaly_word": "人的ション",
     "span_before": "グループ会社の人的ションレポート",
     "answer_text": "人的資本",           "hypothesis": ""},
    {"review_index": 51,   "anomaly_word": "リンクでもダメ",
     "span_before": "リンクでもダメかな",
     "answer_text": "削除",               "hypothesis": ""},
    {"review_index": 54,   "anomaly_word": "ここに楽しんで",
     "span_before": "さん、ここに楽しんでなんか話すことあります",
     "answer_text": "削除",               "hypothesis": ""},
]


# ── comparison helpers ────────────────────────────────────────────────────────

def _item_summary(it: dict) -> str:
    return (
        f"#{it['review_index']} anomaly={it['anomaly_word']!r} "
        f"answer={it['answer_text']!r}"
    )


def compare_items(auto: list[dict], manual: list[dict]) -> None:
    """Print diff between auto and manual ANSWER_ITEMS."""
    # Build lookup: base review_index (strip -N suffix and 'a'/'b') → first matching auto item
    auto_by_base: dict[str, dict] = {}
    for it in auto:
        base = str(it["review_index"]).split("-")[0].rstrip("ab")
        auto_by_base.setdefault(base, it)

    man_by_base: dict[str, dict] = {}
    for it in manual:
        base = str(it["review_index"]).rstrip("ab")
        man_by_base[base] = it

    matched = []
    field_diffs = []
    missing_from_auto = []

    for base, man_it in man_by_base.items():
        if base not in auto_by_base:
            missing_from_auto.append(man_it)
            continue
        auto_it = auto_by_base[base]
        fd = []
        for k in ("anomaly_word", "answer_text"):
            if auto_it.get(k) != man_it.get(k):
                fd.append(f"    {k}: auto={auto_it.get(k)!r}  manual={man_it.get(k)!r}")
        if auto_it.get("span_before") != man_it.get("span_before"):
            fd.append(
                f"    span_before (result-equivalent):\n"
                f"      auto  ={auto_it.get('span_before')!r}\n"
                f"      manual={man_it.get('span_before')!r}"
            )
        if fd:
            field_diffs.append((man_it["review_index"], fd))
        else:
            matched.append(base)

    print(f"=== ANSWER_ITEMS 比較 ===")
    print(f"auto total   : {len(auto)} items")
    print(f"manual total : {len(manual)} items")
    print(f"exact match  : {len(matched)}")
    if field_diffs:
        print(f"field diffs  : {len(field_diffs)} (may be result-equivalent via suffix-stripping)")
        for ri, fd in field_diffs:
            print(f"  #{ri}:")
            for d in fd:
                print(d)
    if missing_from_auto:
        print(f"missing from auto: {len(missing_from_auto)}")
        for it in missing_from_auto:
            print(f"  {_item_summary(it)}")
    extra = [it for b, it in auto_by_base.items() if b not in man_by_base]
    if extra:
        print(f"extra in auto: {len(extra)}")
        for it in extra:
            print(f"  {_item_summary(it)}")


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    md_text       = MD_PATH.read_text(encoding="utf-8")
    fixture       = FIXTURE.read_text(encoding="utf-8")
    manual_target = MANUAL_AFTER_QA.read_text(encoding="utf-8")

    # 1. Parse + interpret + bridge (with source_text safety filter)
    parsed    = parse_md_answers(md_text)
    answered  = [p for p in parsed if p.get("answer_text") is not None]
    print(f"Parsed: {len(parsed)} sections, {len(answered)} answered")

    auto_items = _build_auto_items(parsed, source_text=fixture)
    print(f"Auto ANSWER_ITEMS built: {len(auto_items)}")
    print()

    # 2. Compare with manual ANSWER_ITEMS
    compare_items(auto_items, MANUAL_ITEMS)
    print()

    # 3. Apply auto items from fixture
    print("=== 自動 apply (fixture → auto_after_qa) ===")
    auto_after_qa, log = apply_answers(fixture, auto_items, chat_order=False)

    applied   = [e for e in log if e.get("verdict_applied") not in ("span_not_found", "keep", None)]
    not_found = [e for e in log if e.get("verdict_applied") == "span_not_found"]
    kept      = [e for e in log if e.get("verdict_applied") == "keep"]

    print(f"applied       : {len(applied)}")
    print(f"skipped(keep) : {len(kept)}")
    print(f"span_not_found: {len(not_found)}")
    if not_found:
        for e in not_found:
            print(f"  #{e.get('review_index')} {e.get('anomaly_word')!r}")
    print()

    # 4. Diff auto vs manual after_qa
    print("=== after_qa 比較 ===")
    print(f"auto_after_qa  : {len(auto_after_qa)} chars")
    print(f"manual_after_qa: {len(manual_target)} chars")

    if auto_after_qa == manual_target:
        print("EXACT MATCH ✓ — 自動 apply の結果は手動 apply と完全一致")
    else:
        print("MISMATCH ✗ — 差分:")
        import difflib
        diff = list(difflib.unified_diff(
            manual_target.splitlines(keepends=True),
            auto_after_qa.splitlines(keepends=True),
            fromfile="manual_after_qa",
            tofile="auto_after_qa",
            n=2,
        ))
        print(f"  diff lines: {len(diff)}")
        for line in diff[:80]:
            print("  " + line, end="")
        if len(diff) > 80:
            print(f"\n  ... ({len(diff)-80} more lines)")
    print()

    # 5. Fact gate
    print("=== Fact Gate ===")
    gate = verify_fact_integrity(fixture, auto_after_qa)
    # amounts_missing:['5万円'] is an AUTHORIZED change (MD answer #8a: 5万円→52.5万円)
    authorized_violations = {"amounts_missing:['5万円']"}
    unexpected_violations = [v for v in gate.violations if v not in authorized_violations]
    gate_ok = len(unexpected_violations) == 0
    status = "PASS ✓" if gate_ok else "FAIL ✗"
    print(f"fact gate: {status}")
    for v in gate.violations:
        tag = " (authorized: #8a MD answer)" if v in authorized_violations else " ← UNEXPECTED"
        print(f"  violation: {v}{tag}")
    if gate.warnings:
        for w in gate.warnings:
            print(f"  WARNING: {w}")

    text_match = auto_after_qa == manual_target
    success = text_match and gate_ok
    print()
    print("=== 総合判定 ===")
    print(f"  テキスト一致   : {'✓' if text_match else '✗'}")
    print(f"  Fact gate     : {'✓' if gate_ok else '✗'}")
    print(f"  自動化検証     : {'SUCCESS ✓' if success else 'FAIL ✗'}")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
