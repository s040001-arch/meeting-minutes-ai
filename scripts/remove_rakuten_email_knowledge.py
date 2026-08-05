#!/usr/bin/env python3
"""手動登録した楽天メール由来のナレッジ6件を削除する（E2Eテストをクリーンにするため）。

ユーザーがLINE経由の事前情報取り込み機能を実際に体験するにあたり、
事前に手動登録した知識が残っていると効果検証にならないため削除する。
"""
from __future__ import annotations

from knowledge_sheet_store import load_knowledge_memos, save_knowledge_memos

# add_rakuten_email_knowledge.py で追加した6件（完全一致で削除）
TARGETS = [
    "山屋さんは楽天アド＆メディアカンパニーの新卒研修担当者。音声認識で「山谷」「山家」などと誤認識されることがある。",
    "楽天アド＆メディアカンパニーにはリンクシェア・楽天インサイト・広告事業の3事業があり、2025年新卒社員が2026年10月1日付で配属予定。配属後2〜3か月の集合研修を経て本配属先を決定する。",
    "楽天の2025年新卒社員は入社後約1年半、全国の楽天モバイルショップに配属され、多くが店長として売上目標・KGI・KPI管理、契約獲得施策、スタッフ育成・マネジメント、顧客対応を担当してきた。",
    "楽天の新卒社員は毎朝、三木谷社長との会議に参加し、経営トップへの報告や質疑応答を経験している。",
    "楽天では2026年8〜9月を事前学習期間とし、Udemy Businessでマーケティング基礎・ロジカルシンキング・マーケティングリサーチ・データ分析・ウェブ広告・法人営業・資料作成などの講座を新卒に受講させる予定。",
    "楽天は2026年10月以降の新卒集合研修として、各事業（リンクシェア・楽天インサイト・広告事業）を一定期間ずつ経験するインターンシップ型ローテーションプログラムを検討しており、思考力・基礎スキルを高める集合型研修の提案をプレセナに相談している。",
]


def main() -> int:
    existing = load_knowledge_memos()
    print(f"before={len(existing)}")
    remaining = [m for m in existing if m not in TARGETS]
    removed = len(existing) - len(remaining)
    if removed:
        save_knowledge_memos(remaining)
    print(f"removed={removed} after={len(remaining)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
