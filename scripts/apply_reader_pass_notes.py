"""Reader pass 回答を [補足: …] 注釈として ai.txt に pinpoint 挿入する。

本文を書き換えない・LLMに織り込ませない原則を守り、
該当テキスト直後への文字列挿入のみ行う。
"""
from __future__ import annotations

import pathlib
import re
import sys


def parse_answers(md_path: pathlib.Path) -> list[dict]:
    """MD の回答欄を {rank, anchor_hint, answer} のリストとして返す。空欄はスキップ。"""
    text = md_path.read_text(encoding="utf-8")
    items = []
    # 各 ## # ブロックを分割
    blocks = re.split(r"^## #(\d+)", text, flags=re.MULTILINE)
    # blocks: ['intro', '1', 'block1', '2', 'block2', ...]
    for i in range(1, len(blocks) - 1, 2):
        rank = int(blocks[i])
        body = blocks[i + 1]
        # 該当テキスト（引用行）
        anchor_m = re.search(r"^\> (.+)", body, re.MULTILINE)
        anchor_hint = anchor_m.group(1).strip() if anchor_m else ""
        # 回答欄
        answer_m = re.search(r"^→ 回答[:：]\s*(.+)", body, re.MULTILINE)
        if not answer_m:
            continue
        answer = answer_m.group(1).strip()
        if not answer:
            continue
        items.append({"rank": rank, "anchor_hint": anchor_hint, "answer": answer})
    return items


# 各件: (文字列検索キー, 挿入はそのキー末尾の直後)
# ai.txt の実テキストに合わせた挿入アンカー
ANCHORS: list[tuple[int, str]] = [
    (1, "何形式ですか?すいません、"),
    (2, "起こっちゃってるってことなんですか?"),
    (3, "やっぱり1/3ぐらい。"),
    (4, "最初やってみてもいいかもしれないです。"),
    (5, "頭結構そんな問題なさそうで、その次じゃあもう1段広げて何でしょう?"),
]


def apply_notes(
    src_path: pathlib.Path,
    dst_path: pathlib.Path,
    answers: list[dict],
) -> list[dict]:
    """src をコピーして注釈を挿入し dst に保存。変更サマリを返す。"""
    text = src_path.read_text(encoding="utf-8")
    original_len = len(text)

    answer_map = {a["rank"]: a["answer"] for a in answers}
    report = []

    # 後ろから挿入するため offset がずれないよう逆順で処理
    insertions: list[tuple[int, int, str]] = []  # (pos, rank, note)
    for rank, anchor in ANCHORS:
        answer = answer_map.get(rank)
        if not answer:
            continue
        idx = text.find(anchor)
        if idx < 0:
            report.append({"rank": rank, "status": "ANCHOR_NOT_FOUND", "anchor": anchor})
            continue
        insert_pos = idx + len(anchor)
        note = f" [補足: {answer}]"
        insertions.append((insert_pos, rank, note))

    # 後ろから適用
    for insert_pos, rank, note in sorted(insertions, key=lambda x: -x[0]):
        anchor_for_rank = next(a for r, a in ANCHORS if r == rank)
        idx = text.rfind(anchor_for_rank, 0, insert_pos)
        before_ctx = text[max(0, insert_pos - 30):insert_pos]
        after_ctx = text[insert_pos:insert_pos + 30]
        text = text[:insert_pos] + note + text[insert_pos:]
        report.append({
            "rank": rank,
            "status": "inserted",
            "insert_pos": insert_pos,
            "before": before_ctx + "《挿入点》" + after_ctx,
            "inserted": note,
        })

    dst_path.write_text(text, encoding="utf-8")
    added = len(text) - original_len
    return report, original_len, len(text), added


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    root = pathlib.Path(__file__).resolve().parents[1]
    md_path = root / "scripts/fixtures/reader_pass_20260701_questions.md"
    src_path = root / "scripts/fixtures/job_20260701_053826_ai.txt"
    dst_path = root / "scripts/fixtures/job_20260701_053826_ai_with_notes.txt"

    answers = parse_answers(md_path)
    print(f"回答件数: {len(answers)}")
    for a in answers:
        print(f"  #{a['rank']}: {a['answer'][:40]}...")

    report, orig_len, new_len, added = apply_notes(src_path, dst_path, answers)

    print(f"\n文字数: {orig_len} → {new_len} (+{added}字)")
    print(f"保存先: {dst_path}")
    print()
    for r in sorted(report, key=lambda x: x["rank"]):
        print(f"--- #{r['rank']} [{r['status']}] ---")
        if r["status"] == "inserted":
            print(f"  挿入前: ...{r['before']}...")
            print(f"  挿入注釈: {r['inserted']}")
        else:
            print(f"  anchor: {r.get('anchor')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
