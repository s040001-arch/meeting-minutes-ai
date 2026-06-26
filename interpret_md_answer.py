"""Interpret raw answer_text (from parse_md_answers) into apply action.

action values
-------------
keep          - transcript unchanged (正しい / スキップ / のままでOK etc.)
delete        - delete the whole span_before (answer="削除")
delete_phrase - delete a sub-phrase within the span (keeps surrounding text)
replace       - replace anomaly_word with `result["word"]`
error         - ambiguous; must be resolved before apply

For delete_phrase the deleted string is in result["phrase"].
For replace the substitution word is in result["word"].
"""
from __future__ import annotations

import re

# ── keep ─────────────────────────────────────────────────────────────────────

_KEEP_EXACT = frozenset({
    "正しい語", "正しい", "スキップ", "skip",
})
# ends-with tests (lower-cased)
_KEEP_SUFFIX = (
    "でok",          # 「1000万」でOK / 「神田部長」でOK
    "のままでok",    # 「MC」のままでOK
    "そのままでok",  # そのままでOK
    "のまま",        # 〜のまま
)


def _is_keep(text: str) -> bool:
    t = text.strip()
    if t in _KEEP_EXACT or t.lower() in _KEEP_EXACT:
        return True
    tl = t.lower()
    return any(tl.endswith(s) for s in _KEEP_SUFFIX)


# ── delete ────────────────────────────────────────────────────────────────────

_DELETE_EXACT = frozenset({"削除", "全削除"})

# 「X」削除 (bare, NO は/を) → whole-span delete
_DELETE_BARE_QUOTE_RE = re.compile(r'^[「『](.+?)[」』]削除\s*$')

# 「phrase」を削除 / 「phrase」は削除 → partial phrase deletion
_DELETE_PHRASE_RE = re.compile(r'[「『](.+?)[」』](?:を|は)削除')


def _classify_delete(text: str) -> dict | None:
    t = text.strip()
    if t in _DELETE_EXACT:
        return {"action": "delete"}
    # Bare 「X」削除 (no は/を) → treat as full-span delete
    if _DELETE_BARE_QUOTE_RE.match(t):
        return {"action": "delete"}
    # 「X」を/は削除 → partial phrase deletion
    m = _DELETE_PHRASE_RE.search(t)
    if m:
        return {"action": "delete_phrase", "phrase": m.group(1)}
    return None


# ── replace ───────────────────────────────────────────────────────────────────

# 「sentence」と修正して  ─ corrected-sentence form; need to extract the replacement
_SENTENCE_CORRECTION_RE = re.compile(r'[「『](.+?)[」』]と(?:修正し|変更し|直し)て')

# Numeric amount (万円 / 万 / 千円 / 円)
_AMOUNT_RE = re.compile(r'\d+(?:[,，]\d+)*(?:\.\d+)?\s*(?:万円|万|千円|円)')

# 「X」 at start of text (simple quoted replacement)
_QUOTED_START_RE = re.compile(r'^[「『](.+?)[」』]')


def _extract_hint_from_sentence(sentence: str, anomaly_word: str) -> str | None:
    """From a corrected sentence, extract the word that replaced anomaly_word.

    Strategy: find the first numeric-amount token, which covers cases like
    '52.5万円' replacing '5万円'. Extend to other token types as needed.
    """
    amounts = _AMOUNT_RE.findall(sentence)
    if amounts:
        return amounts[0].strip()
    return None


def _classify_replace(text: str, anomaly_word: str) -> dict:
    t = text.strip()

    # 「sentence」と修正して → extract hint
    m = _SENTENCE_CORRECTION_RE.search(t)
    if m:
        sentence = m.group(1)
        hint = _extract_hint_from_sentence(sentence, anomaly_word)
        if hint:
            return {"action": "replace", "word": hint}
        return {
            "action": "error",
            "message": (
                f"と修正してパターンだが置換語を抽出できない: "
                f"「{sentence}」 (anomaly={anomaly_word!r})"
            ),
        }

    # 「X」で / 「X」です / 「X」にしてください... → extract quoted word
    m = _QUOTED_START_RE.match(t)
    if m:
        word = m.group(1).strip()
        return {"action": "replace", "word": word}

    # Bare text (no brackets).
    # Strip trailing 。 so suffix-stripping in replacement_word() can fire.
    bare = t.rstrip("。")
    if bare:
        return {"action": "replace", "word": bare}

    return {"action": "error", "message": f"置換語を抽出できない: {text!r}"}


# ── public API ────────────────────────────────────────────────────────────────

def interpret_answer(answer_text: str | None, anomaly_word: str = "") -> dict:
    """Classify a raw MD answer into an apply action.

    Parameters
    ----------
    answer_text : raw text from ### 回答 block (output of parse_md_answers)
    anomaly_word: the anomaly word from the same section header (used for
                  hint extraction in sentence-correction patterns)

    Returns
    -------
    dict with:
        "action" : "keep" | "delete" | "delete_phrase" | "replace" | "error"
        "word"   : str  (replace only)
        "phrase" : str  (delete_phrase only)
        "message": str  (error only)
    """
    if answer_text is None:
        return {"action": "error", "message": "answer_text is None (未回答)"}

    t = answer_text.strip()

    # Priority: keep > delete > replace
    if _is_keep(t):
        return {"action": "keep"}

    del_result = _classify_delete(t)
    if del_result:
        return del_result

    return _classify_replace(t, anomaly_word=anomaly_word)


# ── __main__ smoke test ───────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.stdout.reconfigure(encoding="utf-8")
    sys.path.insert(0, str(Path(__file__).parent))
    from parse_md_answers import parse_md_answers

    md = (Path(__file__).parent / "docs/phase10_164142_ask_review_combined.md").read_text(
        encoding="utf-8"
    )
    parsed = parse_md_answers(md)

    counts: dict[str, int] = {}
    errors: list[str] = []

    print(f"{'#':>3}  {'anomaly':28}  {'action':14}  word/phrase")
    print("-" * 85)
    for it in parsed:
        ri = it["review_index"]
        aw = it["anomaly_word"]
        result = interpret_answer(it["answer_text"], anomaly_word=aw)
        act = result["action"]
        counts[act] = counts.get(act, 0) + 1

        detail = ""
        if act == "replace":
            detail = result["word"]
        elif act == "delete_phrase":
            detail = f"phrase={result['phrase']!r}"
        elif act == "error":
            detail = result["message"]
            errors.append(f"  #{ri} {aw!r}: {result['message']}")

        print(f"{ri:3d}  {aw!r:28s}  {act:14s}  {detail}")

    print()
    print("=== 集計 ===")
    for act in ("keep", "replace", "delete", "delete_phrase", "error"):
        n = counts.get(act, 0)
        if n:
            print(f"  {act:14s}: {n}")
    if errors:
        print()
        print("=== ERROR ===")
        for e in errors:
            print(e)
    else:
        print("\nerror 0 ✓")
