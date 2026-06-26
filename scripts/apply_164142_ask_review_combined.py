#!/usr/bin/env python3
"""Apply phase10_164142_ask_review_combined.md answers to data/164142_after_qa.txt.

Rules:
- anomaly_word のみ置換・周辺不変（pinpoint apply infrastructure）
- 削除系: span 全体を削除
- keep/スキップ系: no-op
- 多くの ②③ 項目は step-3 が既に自動適用済み → fixture に anomaly_word が存在しない
  → そういった項目は SKIP (already_corrected) として報告

SOURCE: data/fixtures/job_164142_step3/merged_transcript_after_qa.txt (step-3 後の pristine 状態)
TARGET: data/164142_after_qa.txt (in-place 上書き)

Does NOT use LLM incorporate.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pinpoint_answer_apply import apply_answers  # noqa: E402

# SOURCE: pristine state after step-3 (before any QA apply)
SOURCE = ROOT / "data/fixtures/job_164142_step3/merged_transcript_after_qa.txt"
# TARGET: destination to overwrite
TARGET = ROOT / "data/164142_after_qa.txt"

# -------------------------------------------------------------------------
# ANSWER_ITEMS: 16 items confirmed to exist in the SOURCE fixture.
#
# Items NOT listed here are either:
#   a) already corrected by step-3 auto_correct/auto_delete (ABSENT from fixture)
#   b) exist only in wrong context (e.g. 創業 only in 柳掃除創業) → intentionally omitted
#   c) anomaly_word is a substring of already-correct text (e.g. 本さん inside 秋本さん)
#
# Fields:
#   review_index  : MD section number
#   anomaly_word  : text to locate within span_before (may equal span_before for phrase-delete)
#   span_before   : text used to locate the edit site in SOURCE
#   answer_text   : replacement string, or "削除" for span deletion,
#                   or corrected-span text for phrase-delete cases
#   hypothesis    : original ② hypothesis (informational only; replacement_word uses it)
# -------------------------------------------------------------------------
ANSWER_ITEMS: list[dict] = [

    # ── Tier A: 明らかな事実誤り ──────────────────────────────────────────

    # #1 8数字: ABSENT in fixture (already corrected to 勝ち筋 by step-3) → skipped

    # #2 1000車: ABSENT in fixture (already corrected to 1000社 by step-3) → skipped

    # #4 2試合: ABSENT in fixture (already corrected to 2社 by step-3) → skipped

    {
        "review_index": 5,
        "anomaly_word": "映画",
        "span_before": "1 個の映画ですね",
        "answer_text": "シナリオ",
        "hypothesis": "",
    },
    {
        "review_index": 6,
        "anomaly_word": "5 月に行きやつ",
        "span_before": "5 月に行きやつ行きますよね",
        # suffix-strip: anomaly "5 月に行きやつ" → "5 月に" avoids duplicating "行きますよね"
        "answer_text": "5 月に",
        "hypothesis": "",
    },
    # #7 古車(target span "…古車で調査開始"): ABSENT in fixture (step-3 already fixed)
    # Only remaining "古車" in fixture is in unrelated context → intentionally omitted

    {
        "review_index": "8a",
        "anomaly_word": "5万円",
        "span_before": "5万円そうすよね。半日ですよね",
        "answer_text": "52.5万円",   # MD回答: 「そうすると多分52.5万円でできるし」と修正して (hypothesis 25万円 は不採用)
        "hypothesis": "25万円",
    },
    # #8b 52。5万円: ABSENT in fixture (already corrected to 52.5万円 by step-3) → skipped

    # #10 ロ自身(target "2年間ぐらいロ自身"): ABSENT in fixture → skipped
    # Remaining "ロ自身" occurrences are in different context (not authorized by proposal)

    # #11 23科目: ABSENT in fixture → skipped

    # #12 TOKIO(target "私と TOKIO さん"): ABSENT in fixture (already → 季央 by step-3) → skipped

    # #13 HL: ABSENT in fixture → skipped

    # #15 特殊: FP 事業の中で を削除し "真面目に" を残す
    #    anomaly_word = span_before = "FP 事業の中で真面目に" → answer = "真面目に"
    #    replacement_word returns "真面目に" as the replacement for the whole span
    {
        "review_index": 15,
        "anomaly_word": "FP 事業の中で真面目に",
        "span_before": "FP 事業の中で真面目に",
        "answer_text": "真面目に",
        "hypothesis": "",
    },

    # #16 iPhone さん: ABSENT in fixture (target context already corrected) → skipped

    # ── Tier B: 本文影響あり ──────────────────────────────────────────────

    # #17 シノベーション: ABSENT in fixture → skipped

    {
        "review_index": 18,
        "anomaly_word": "鋭角",
        "span_before": "豊通でまた鋭角と言われている",
        "answer_text": "A格",
        "hypothesis": "",
    },
    # #19 三菱商人たち: ABSENT in fixture → skipped

    # #20 創業: target span ABSENT; only remaining "創業" is in 柳掃除創業 (skip #31) → omitted

    {
        "review_index": 21,
        "anomaly_word": "小松放棄",
        "span_before": "小松放棄もやりましたね",
        "answer_text": "小松鋼機",
        "hypothesis": "",
    },
    {
        "review_index": 22,
        "anomaly_word": "松コープ",
        "span_before": "松コープと矢崎",
        "answer_text": "小松鋼機",
        "hypothesis": "",
    },
    # #23 特殊: "ビジネスがワンマン不協さん、" を削除し "秋本さん" と "あの開発" を保持
    #    answer = "秋本さんあの開発" → replacement_word strips prefix "秋本さん" + suffix "あの開発"
    #    → replacement word = "" → span_after = "秋本さんあの開発"
    {
        "review_index": 23,
        "anomaly_word": "ビジネスがワンマン不協さん、",
        "span_before": "秋本さんビジネスがワンマン不協さん、あの開発",
        "answer_text": "秋本さんあの開発",
        "hypothesis": "",
    },
    # #25a 松本さん: ABSENT in fixture → skipped
    # #25b 本さん: target span ABSENT; "本さん" remaining is substring of "秋本さん" → omitted
    # #26 サンタさん: ABSENT in fixture → skipped
    # #27 駆動車: ABSENT in fixture → skipped

    # #30 特殊: span in fixture is "根本さんがベース 3 を作ってくれてた" (梅本→根本 by step-3)
    {
        "review_index": 30,
        "anomaly_word": "ベース 3",
        "span_before": "根本さんがベース 3 を作ってくれてた",
        "answer_text": "ベース",
        "hypothesis": "",
    },

    # ── Tier C: 低材料性 ─────────────────────────────────────────────────

    # #35 次元コンサル: ABSENT in fixture → skipped

    {
        "review_index": 36,
        "anomaly_word": "儲かりのフェア",
        "span_before": "切り口で儲かりのフェアをやりまなぞ。なぞ対策を議論していく",
        "answer_text": "Where",
        "hypothesis": "",
    },
    {
        "review_index": 37,
        "anomaly_word": "予兆会社",
        "span_before": "予兆会社そうなんすよ",
        "answer_text": "削除",
        "hypothesis": "",
    },
    # #38 特殊: anomaly_word = span_before = 削除対象フレーズ全体 → answer = "削除"
    {
        "review_index": 38,
        "anomaly_word": "者向け見解者向けてはい下だしからのえ、",
        "span_before": "者向け見解者向けてはい下だしからのえ、",
        "answer_text": "削除",
        "hypothesis": "",
    },
    {
        "review_index": 39,
        "anomaly_word": "バイかけたい",
        "span_before": "盆前にバイかけたい",
        "answer_text": "かけたい",
        "hypothesis": "",
    },
    # #41 新キル: ABSENT in fixture (already deleted by step-3) → skipped

    {
        "review_index": 47,
        "anomaly_word": "人的ション",
        "span_before": "グループ会社の人的ションレポート",
        "answer_text": "人的資本",
        "hypothesis": "",
    },
    # #48 女将: ABSENT in fixture (already corrected to 御上 by step-3) → skipped
    # #50 78割: ABSENT in fixture (already corrected to 7、8割 by step-3) → skipped

    {
        "review_index": 51,
        "anomaly_word": "リンクでもダメ",
        "span_before": "リンクでもダメかな",
        "answer_text": "削除",
        "hypothesis": "",
    },
    {
        "review_index": 54,
        "anomaly_word": "ここに楽しんで",
        "span_before": "さん、ここに楽しんでなんか話すことあります",
        "answer_text": "削除",
        "hypothesis": "",
    },
]

# Items corrected by step-3 (for reporting)
STEP3_ALREADY_CORRECTED = [
    (1,  "8 数字",        "勝ち筋"),
    (2,  "1000 車",       "1000 社"),
    (4,  "2 試合",        "2 社"),
    (7,  "古車",          "5社（step-3）"),
    ("8b","52。5万円",    "52.5万円"),
    (10, "ロ自身",        "ロジシン（目標span消失）"),
    (11, "23 科目",       "2、3 科目"),
    (12, "TOKIO",        "季央"),
    (13, "HL",           "HR"),
    (16, "iPhone さん",  "相原さん"),
    (17, "シノベーション", "CCイノベーション"),
    (19, "三菱商人たち",  "三菱商事"),
    (20, "創業",          "季央さんはい（target消失）"),
    ("21a","松本さん",    "秋本さん"),
    ("21b","本さん",      "秋本さん（target消失）"),
    (22, "サンタさん",    "猿田さん"),
    (23, "駆動車",        "工藤さん"),
    (25, "次元コンサル",  "地銀コンサル"),
    (29, "バイかけたい(tgt span)", "span変化"),
    (30, "新キル",        "削除済み"),
    (32, "女将",          "御上"),
    (33, "78 割",         "7、8 割"),
]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")

    if not SOURCE.is_file():
        print(f"ERROR: source not found: {SOURCE}", file=sys.stderr)
        return 1

    original = SOURCE.read_text(encoding="utf-8")
    print(f"SOURCE (fixture): {len(original)} chars, {original.count(chr(10))} lines")
    print(f"Applying {len(ANSWER_ITEMS)} answer items...")

    new_text, applied_log = apply_answers(original, ANSWER_ITEMS)

    # ── Report ──────────────────────────────────────────────────────────
    changed: list[dict] = []
    errors: list[dict] = []
    skipped: list[dict] = []

    for entry in applied_log:
        if entry.get("error"):
            errors.append(entry)
        elif entry.get("skipped"):
            skipped.append(entry)
        else:
            changed.append(entry)

    print()
    print("=== 適用結果 ===")
    print(f"  applied (変更あり): {len(changed)}")
    print(f"  skipped (keep/no-op): {len(skipped)}")
    print(f"  errors:             {len(errors)}")

    if errors:
        print()
        print("=== ERRORS ===")
        for e in errors:
            print(f"  #={e.get('review_index')} anomaly={e.get('anomaly_word')!r}  {e.get('error')}")

    print()
    print("=== before/after (変更箇所) ===")
    for c in changed:
        ri = c.get("review_index")
        aw = c.get("anomaly_word", "")
        sb = c.get("span_before", "")
        sa = c.get("span_after")
        mode = c.get("apply_mode", "")
        print(f"\n--- #={ri}  anomaly={aw!r}  mode={mode} ---")
        print(f"  BEFORE: {sb!r}")
        if sa is None:
            print(f"  AFTER:  [deleted]")
        else:
            print(f"  AFTER:  {sa!r}")
        if c.get("apply_meta", {}).get("over_replaced"):
            print(f"  *** over_replaced flag ***")

    print()
    print(f"=== step-3 で既適用済み({len(STEP3_ALREADY_CORRECTED)}件) ===")
    for ri, aw, correction in STEP3_ALREADY_CORRECTED:
        print(f"  #={ri} {aw!r} → {correction}")

    # ── Verify ──────────────────────────────────────────────────────────
    print()
    print("=== 検証 ===")
    delta = len(new_text) - len(original)
    print(f"  文字数: {len(original)} → {len(new_text)} (Δ{delta:+d})")

    # 過削除チェック: should-remain words
    guard_words = [
        "北陸小松", "1000万", "神田部長", "野村", "MC",
        "矢崎", "秋本さん", "工藤さん", "相原",
        "柳掃除",  # should remain (スキップ #31)
        "なんかった系",  # skip #53
    ]
    over_delete_flags: list[str] = []
    for w in guard_words:
        if w not in new_text:
            over_delete_flags.append(w)
    if over_delete_flags:
        print(f"  WARN: 過削除の疑い: {over_delete_flags}")
    else:
        print(f"  OK: 過削除チェック PASS ({len(guard_words)} 語すべて残存)")

    # 修正後の語が存在すること
    expect_present = [
        "シナリオ", "A格", "小松鋼機", "人的資本", "Where",
    ]
    missing_after = [w for w in expect_present if w not in new_text]
    if missing_after:
        print(f"  WARN: 修正後の語が見つからない: {missing_after}")
    else:
        print(f"  OK: 修正後語 存在チェック PASS")

    # 修正対象語が消えていること (this-script items only)
    expect_gone_this_script = [
        "映画", "5 月に行きやつ", "FP 事業の中で真面目に",
        "鋭角", "小松放棄", "松コープ", "儲かりのフェア",
        "予兆会社", "人的ション", "リンクでもダメ", "ここに楽しんで",
    ]
    still_present = [w for w in expect_gone_this_script if w in new_text]
    if still_present:
        print(f"  WARN: まだ残っている語: {still_present}")
    else:
        print(f"  OK: 修正対象語 消去チェック PASS")

    # 不協さん確認 (部分削除: "ビジネスがワンマン不協さん、" が消え "秋本さん" が残る)
    if "秋本さんあの開発" in new_text and "ビジネスがワンマン不協さん" not in new_text:
        print(f"  OK: 不協さん句削除 PASS (秋本さんあの開発 → 残存)")
    elif "ビジネスがワンマン不協さん" in new_text:
        print(f"  WARN: ビジネスがワンマン不協さん が未削除")
    else:
        # check partial
        if "秋本さん" in new_text:
            print(f"  OK: 秋本さん残存, 不協さん削除完了")

    # ── Write ────────────────────────────────────────────────────────────
    if errors:
        print(f"\nINFO: {len(errors)} item(s) had errors (likely already corrected by step-3 or span changed)")

    TARGET.write_text(new_text, encoding="utf-8")
    print(f"\n書き込み完了: {TARGET}")
    print(f"  {len(original)} → {len(new_text)} chars (Δ{delta:+d})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
