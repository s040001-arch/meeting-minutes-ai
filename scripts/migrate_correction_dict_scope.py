#!/usr/bin/env python3
"""correction_dict.json の危険な盲目置換エントリを学習辞書 scope=context へ移行する。

背景 (2026-07-08 デイシス案件):
    correction_dict.json は機械補正(Step 4.2)で全ジョブに無条件適用される。
    「富士山→藤井さん」のような実在語の置換が LINE webhook 経由で無審査に
    追加されており、別会議の本文を汚染していた（富士山→藤井さんが以前構成…）。

処理:
    1. data/correction_dict.json をバックアップ
    2. 各エントリを learned_corrections_store.suggest_scope で判定
       - "context"（実在語・3文字以下の短い語）→ dict から削除し、
         学習辞書に scope=context で記録（coherence 検出ヒントとして継続利用）
       - "global"（実在しにくい崩れ表記）→ dict に残す
    3. 保存

ローカル/Railway 共通（cwd = リポジトリルートで実行）。
"""
from __future__ import annotations

import io
import json
import shutil
import sys
from datetime import date
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from learned_corrections_store import (  # noqa: E402
    add_learned_correction,
    suggest_scope,
)

DICT_PATH = REPO / "data" / "correction_dict.json"


def main() -> int:
    if not DICT_PATH.is_file():
        print(f"correction_dict_missing={DICT_PATH}")
        return 0

    data = json.loads(DICT_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        print("correction_dict_not_a_dict")
        return 1

    backup = DICT_PATH.with_name(
        f"correction_dict.backup_{date.today().strftime('%Y%m%d')}.json"
    )
    if not backup.exists():
        shutil.copyfile(DICT_PATH, backup)
        print(f"backup={backup.name}")

    kept: dict[str, str] = {}
    migrated = 0
    conflicts = 0
    for wrong, right in data.items():
        w = str(wrong).strip()
        r = str(right).strip()
        if not w or not r:
            continue
        if suggest_scope(w) == "global":
            kept[w] = r
            continue
        res = add_learned_correction(
            wrong=w,
            right=r,
            via="dict_migration",
            job_id="",
            confidence="high",
            scope="context",
        )
        action = res.get("action")
        if action in ("added", "updated"):
            migrated += 1
            print(f"migrated: {w!r} -> {r!r} ({action})")
        else:
            # 学習辞書側と矛盾していても、盲目置換の危険は同じなので dict からは外す
            conflicts += 1
            print(f"removed_only: {w!r} -> {r!r} (learned={res.get('reason')})")

    DICT_PATH.write_text(
        json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"done total={len(data)} kept_global={len(kept)} "
        f"migrated_context={migrated} removed_only={conflicts}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
