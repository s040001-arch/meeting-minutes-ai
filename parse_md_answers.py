"""Parse phase10 combined-review MD to extract per-section answers.

Each section of the MD looks like:

    ## 8. [A_明らかな事実誤り] 5万円 ② × 2件
    ...
    ### 回答
    > ②候補: 「25万円」→ 「正しい」か別の語か削除を記入
    >
    > *(「そうすると多分52.5万円でできるし」と修正して)*
    ---

The function parse_md_answers() extracts:
    review_index  : int  (section number)
    anomaly_word  : str  (the garbled word being reviewed)
    answer_text   : str  (raw text inside > *(...)*), or None if not answered
    parse_error   : str  (only present when something went wrong)
"""
from __future__ import annotations

import re

# ## 8. [A_明らかな事実誤り] 5万円 ② × 2件
_HEADER_RE = re.compile(
    r"^## (\d+)\.\s+\[([^\]]+)\]\s+(.+?)\s+(②|③)(?:\s*×\s*\d+件)?\s*$",
    re.MULTILINE,
)

# > *(answer text here)*   — full line
_ANSWER_LINE_RE = re.compile(r"^>\s*\*\((.+?)\)\*\s*$")

# Template placeholder inserted by export script (means "not yet answered")
_TEMPLATE_PLACEHOLDER = "正しい語 / 削除 / スキップ"

# Span extraction helpers
_CODE_BLOCK_RE = re.compile(r"```\n(.+?)\n```", re.DOTALL)
_HIGHLIGHT_RE = re.compile(r"【(.+?)】")


def _parse_spans(section: str) -> list[str]:
    """Extract span_before strings from a section, removing 【anomaly】 highlights.

    Scans between '### span_before' and '### 周辺文脈' to avoid picking up
    context code blocks.
    """
    sb_pos = section.find("### span_before")
    if sb_pos == -1:
        return []
    ctx_pos = section.find("### 周辺文脈", sb_pos)
    if ctx_pos == -1:
        ctx_pos = len(section)
    sb_sec = section[sb_pos:ctx_pos]
    return [
        _HIGHLIGHT_RE.sub(r"\1", m.group(1).strip())
        for m in _CODE_BLOCK_RE.finditer(sb_sec)
    ]


def parse_md_answers(md_text: str) -> list[dict]:
    """Parse the combined review MD and return one dict per section.

    Each dict has:
        review_index : int
        anomaly_word : str
        spans        : list[str]    (span_before strings, 【】 removed)
        answer_text  : str | None   (None = not yet answered or template)
        parse_error  : str          (only present on error)
    """
    results: list[dict] = []
    headers = list(_HEADER_RE.finditer(md_text))

    for i, m in enumerate(headers):
        review_index = int(m.group(1))
        anomaly_word = m.group(3).strip()

        # Slice this section (up to the next header)
        start = m.start()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(md_text)
        section = md_text[start:end]

        spans = _parse_spans(section)

        # Find "### 回答" block
        ans_pos = section.find("### 回答")
        if ans_pos == -1:
            results.append({
                "review_index": review_index,
                "anomaly_word": anomaly_word,
                "spans": spans,
                "answer_text": None,
                "parse_error": "no_answer_section",
            })
            continue

        answer_block = section[ans_pos:]

        # Collect all > *(...)*  lines (template line comes first, actual answer last)
        raw_matches: list[str] = []
        for line in answer_block.splitlines():
            am = _ANSWER_LINE_RE.match(line)
            if am:
                raw_matches.append(am.group(1).strip())

        if not raw_matches:
            results.append({
                "review_index": review_index,
                "anomaly_word": anomaly_word,
                "spans": spans,
                "answer_text": None,
                "parse_error": "no_answer_text",
            })
            continue

        # The actual answer is always the LAST > *(...)*  match.
        # (For ② sections the template "②候補: ..." is NOT in > *()* form, so
        #  raw_matches typically has exactly 1 entry; the placeholder check still
        #  handles the edge case where the MD was left unfilled.)
        raw_answer = raw_matches[-1]

        if raw_answer == _TEMPLATE_PLACEHOLDER:
            results.append({
                "review_index": review_index,
                "anomaly_word": anomaly_word,
                "spans": spans,
                "answer_text": None,
                "parse_error": "template_not_filled",
            })
            continue

        results.append({
            "review_index": review_index,
            "anomaly_word": anomaly_word,
            "spans": spans,
            "answer_text": raw_answer,
        })

    return results


if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.stdout.reconfigure(encoding="utf-8")
    md_path = Path(__file__).parent / "docs/phase10_164142_ask_review_combined.md"
    text = md_path.read_text(encoding="utf-8")
    items = parse_md_answers(text)

    errors = [it for it in items if "parse_error" in it]
    answered = [it for it in items if it.get("answer_text") is not None]

    print(f"sections parsed : {len(items)}")
    print(f"answered        : {len(answered)}")
    print(f"parse errors    : {len(errors)}")
    if errors:
        for e in errors:
            print(f"  #{e['review_index']} {e['anomaly_word']!r}: {e['parse_error']}")
    print()
    print("sample (first 5 answered):")
    for it in answered[:5]:
        print(f"  #{it['review_index']:2d}  {it['anomaly_word']!r:30s}  -> {it['answer_text']!r}")
    print("  ...")
    # spot-check #8 (should be 52.5万円 in answer text)
    item8 = next((it for it in items if it["review_index"] == 8), None)
    if item8:
        print(f"\n#8 (5万円 bundle): answer_text = {item8['answer_text']!r}")
