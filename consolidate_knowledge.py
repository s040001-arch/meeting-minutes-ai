"""Knowledge Sheet の重複・類似ナレッジを AI で統合する一回限りのスクリプト。"""
import json
import os
import sys
import time

import anthropic
import httpx

from repo_env import load_dotenv_local
from knowledge_sheet_store import load_knowledge_memos, save_knowledge_memos

_MODEL = "claude-sonnet-4-20250514"
_BATCH_SIZE = 250

_SYSTEM_PROMPT = """\
あなたはナレッジ管理の専門家です。
議事録AIシステムが使うナレッジ一覧（用語・人名・組織名・関係性などの知識）が与えられます。

【タスク】
この一覧を整理・統合して、重複や冗長を排除した最適なナレッジ一覧を出力してください。

【統合ルール】
1. 同じ人物・組織・用語について複数の記述がある場合 → 1件に統合し情報を合成
   例: 「川口氏＝NRE物流事業部」「川口さんは事業企画部所属」→「川口氏＝NRE物流事業部・事業企画部所属」
2. 同じ概念を別の表現で書いているもの → より正確・簡潔な方を残す
3. 一般常識レベルの情報（「M&A＝企業の合併買収」等）→ 削除
4. 特定の会議だけに関係し再利用価値が低いもの → 削除
5. 音声認識の補正に役立つ情報（固有名詞、社内用語、略称）は優先的に残す
6. 「？」「〜的」など推測レベルの記述 → 確定情報のみに絞る

【出力形式】
JSON配列のみを出力。説明文やコードフェンスは付けないでください。
1件＝1行の簡潔な日本語文字列。
"""


def consolidate_batch(client: anthropic.Anthropic, items: list[str], label: str) -> list[str]:
    payload = json.dumps(items, ensure_ascii=False)
    print(f"  [{label}] {len(items)}件を送信中...", flush=True)

    started = time.monotonic()
    full = ""
    with client.messages.stream(
        model=_MODEL,
        max_tokens=8000,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": payload}],
    ) as stream:
        for chunk in stream.text_stream:
            full += chunk
            now = time.monotonic()
            if int(now - started) % 10 == 0 and len(full) % 100 < 5:
                print(f"  [{label}] ...{int(now-started)}秒, {len(full):,}文字受信", flush=True)

    elapsed = time.monotonic() - started
    print(f"  [{label}] 完了 ({elapsed:.0f}秒, {len(full):,}文字)", flush=True)

    raw = full.strip()
    if raw.startswith("```"):
        import re
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        print(f"  [{label}] JSON解析失敗、元データを返却", flush=True)
        return items

    if not isinstance(result, list):
        return items
    return [str(x).strip() for x in result if str(x).strip()]


def main():
    load_dotenv_local()

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("ANTHROPIC_API_KEY is not set.")
        sys.exit(1)

    client = anthropic.Anthropic(
        api_key=api_key,
        timeout=httpx.Timeout(timeout=300.0, connect=30.0),
    )

    print("Knowledge Sheet からナレッジを読み込み中...", flush=True)
    items = load_knowledge_memos()
    print(f"現在のナレッジ数: {len(items)}件", flush=True)

    batches = [items[i:i + _BATCH_SIZE] for i in range(0, len(items), _BATCH_SIZE)]
    print(f"{len(batches)}バッチに分割して統合します\n", flush=True)

    consolidated = []
    for idx, batch in enumerate(batches):
        result = consolidate_batch(client, batch, f"Batch {idx+1}/{len(batches)}")
        print(f"  → {len(batch)}件 → {len(result)}件に統合\n", flush=True)
        consolidated.extend(result)

    if len(batches) > 1:
        print(f"最終統合: {len(consolidated)}件を横断統合中...\n", flush=True)
        consolidated = consolidate_batch(client, consolidated, "最終統合")
        print(f"  → 最終結果: {len(consolidated)}件\n", flush=True)

    print(f"\n=== 結果 ===", flush=True)
    print(f"統合前: {len(items)}件", flush=True)
    print(f"統合後: {len(consolidated)}件", flush=True)
    print(f"削減数: {len(items) - len(consolidated)}件\n", flush=True)

    if "--dry-run" in sys.argv:
        print("(dry-run: Sheet書き込みスキップ)", flush=True)
        with open("data/consolidated_knowledge.json", "w", encoding="utf-8") as f:
            json.dump(consolidated, f, ensure_ascii=False, indent=2)
        print("data/consolidated_knowledge.json に保存しました", flush=True)
    else:
        print("Knowledge Sheet に保存中...", flush=True)
        save_knowledge_memos(consolidated)
        print("Knowledge Sheet を更新しました", flush=True)
        with open("data/consolidated_knowledge.json", "w", encoding="utf-8") as f:
            json.dump(consolidated, f, ensure_ascii=False, indent=2)
        print("data/consolidated_knowledge.json にもバックアップ保存しました", flush=True)


if __name__ == "__main__":
    main()
