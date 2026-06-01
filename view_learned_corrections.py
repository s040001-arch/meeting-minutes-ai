"""学習済み補正辞書を CLI で確認する。

使い方:
  python view_learned_corrections.py                # 全件表示
  python view_learned_corrections.py --path X.json  # パス指定
  python view_learned_corrections.py --remove WORD  # 誤学習エントリの削除
"""
from __future__ import annotations

import argparse
import sys

from learned_corrections_store import (
    DEFAULT_LEARNED_PATH,
    format_for_print,
    remove_learned_correction,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="学習済み補正辞書を表示/編集する")
    parser.add_argument("--path", default=DEFAULT_LEARNED_PATH)
    parser.add_argument(
        "--remove",
        default=None,
        help="指定された wrong 文字列のエントリを削除する(誤学習対処)",
    )
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")

    if args.remove:
        removed = remove_learned_correction(args.remove, path=args.path)
        if removed:
            print(f"removed: {args.remove!r}")
        else:
            print(f"not_found: {args.remove!r}")
        return 0

    print(format_for_print(args.path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
