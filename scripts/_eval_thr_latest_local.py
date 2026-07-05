#!/usr/bin/env python3
"""ローカル取得済みのthr最新after_qaを評価: 回答反映確認と残ガーブル走査。"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

TEXT = Path("data/thr_after_qa_latest_u8.txt").read_text(encoding="utf-8")


def show(word: str, label: str = "", limit: int = 3, half: int = 70) -> None:
    idx = 0
    hits = 0
    while hits < limit:
        i = TEXT.find(word, idx)
        if i < 0:
            break
        hits += 1
        s = max(0, i - half)
        e = min(len(TEXT), i + len(word) + half)
        print(f"  [{label or word}] @{i}: ...{TEXT[s:e]}...".replace("\n", "\\n"))
        idx = i + 1
    if hits == 0:
        print(f"  [{label or word}] not found")


print(f"len={len(TEXT)}")
print("=== A) 6件の回答反映確認（旧文言が残っていないか） ===")
for w in [
    "はめるのを伝え", "もの山のま",              # 1 OK
    "カフェですか", "住んでいる個別契約",        # 2 OK
    "日系である", "連れているものがなくて",      # 3 言い直し
    "ドネと協定", "ベスト上にて",                # 4 OK
    "ホームベース", "合わせていた時のとありがたい",  # 5 言い直し
    "レンズさん", "魔王社",                      # 6 OK
]:
    show(w)

print("\n=== B) 新文言の適用確認 ===")
for w in [
    "メールでお伝えさせていただいた通り",
    "何月に実施するかという個別契約",
    "その日程で合意です、とか、どこか漏れているとか",
    "Box上での協定",
    "他社ではその場合そういったテーマの確定",
    "御社の方であのアンケート",
]:
    show(w)

print("\n=== C) 既知の残ガーブル・不自然表現の走査 ===")
for w in [
    "かしらいるんですけどね", "レースク", "固定はあのあった",
    "とはいなら何で言うとね", "多分名前にはなってないかもしれな",
    "予定をこうシェアしあったりとかってください",
    "先生日程確認書", "NEST", "精神と", "うんこ",
    "いう風にあの思ったところうんいうのと",
    "ってところで", "してるてる", "がなんか、",
]:
    show(w, limit=4)

print("\n=== D) 機械的パターン走査 ===")
# 同語連続（どのようにどのように等）
for m in re.finditer(r"(\S{2,6})\1", TEXT):
    seg = m.group(0)
    if re.fullmatch(r"[ぁ-ん]+", m.group(1)):
        continue
    s = max(0, m.start() - 30)
    e = min(len(TEXT), m.end() + 30)
    print(f"  [重複?] {seg!r}: ...{TEXT[s:e]}...".replace("\n", "\\n"))
