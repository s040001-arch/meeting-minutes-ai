#!/usr/bin/env python3
"""事前メール（山屋様 2026-08 楽天新卒研修）から抽出した知識をナレッジシートに登録する。

議事録処理前に事前資料の文脈を取り込むための即時対応（2026-08-05）。
既存メモとの重複はキーワードで確認し、完全一致は追加しない。
"""
from __future__ import annotations

from knowledge_sheet_store import load_knowledge_memos, save_knowledge_memos

NEW_MEMOS = [
    "山屋さんは楽天アド＆メディアカンパニーの新卒研修担当者。音声認識で「山谷」「山家」などと誤認識されることがある。",
    "楽天アド＆メディアカンパニーにはリンクシェア・楽天インサイト・広告事業の3事業があり、2025年新卒社員が2026年10月1日付で配属予定。配属後2〜3か月の集合研修を経て本配属先を決定する。",
    "楽天の2025年新卒社員は入社後約1年半、全国の楽天モバイルショップに配属され、多くが店長として売上目標・KGI・KPI管理、契約獲得施策、スタッフ育成・マネジメント、顧客対応を担当してきた。",
    "楽天の新卒社員は毎朝、三木谷社長との会議に参加し、経営トップへの報告や質疑応答を経験している。",
    "楽天では2026年8〜9月を事前学習期間とし、Udemy Businessでマーケティング基礎・ロジカルシンキング・マーケティングリサーチ・データ分析・ウェブ広告・法人営業・資料作成などの講座を新卒に受講させる予定。",
    "楽天は2026年10月以降の新卒集合研修として、各事業（リンクシェア・楽天インサイト・広告事業）を一定期間ずつ経験するインターンシップ型ローテーションプログラムを検討しており、思考力・基礎スキルを高める集合型研修の提案をプレセナに相談している。",
]

KEYWORDS = ["山屋", "アド＆メディア", "リンクシェア", "楽天インサイト", "Udemy", "三木谷"]


def main() -> int:
    existing = load_knowledge_memos()
    print(f"existing_memos={len(existing)}")
    print("--- 既存の関連メモ ---")
    for m in existing:
        if any(k in m for k in KEYWORDS):
            print(f"  {m}")
    added = [m for m in NEW_MEMOS if m not in existing]
    if not added:
        print("no new memos to add")
        return 0
    save_knowledge_memos(existing + added)
    print(f"added={len(added)} total={len(existing) + len(added)}")
    for m in added:
        print(f"  + {m[:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
