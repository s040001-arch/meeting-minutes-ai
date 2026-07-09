#!/usr/bin/env python3
"""プレセナ提案レビュー会(物流事業部向けプロマネ研修)で確定した誤変換を学習辞書へ記録する。

- GLOBAL_PAIRS: wrong が実在語として現れない崩れ表記 → 次ジョブから機械補正で自動置換
- CONTEXT_PAIRS: wrong が実在語 → 盲目置換せず、coherence 検出プロンプトの
  「過去のQ&Aで確定した文脈依存ペア」ヒントとして蓄積（文脈が合えば候補付きで検出→質問）

語彙のハードコードではなく学習辞書に載せることで、
次回以降どのジョブでも文脈判定つきで検出される。
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from learned_corrections_store import add_learned_correction  # noqa: E402

JOB_ID = "job_20260708_043610_2026_0708_プレセナ社_提案レビュー会_物流事業部向けプロマネ研修_荻野"
VIA = "chat_fix"

# wrong が崩れ表記でしか現れない → 機械補正で無条件置換してよい
GLOBAL_PAIRS: list[tuple[str, str, str]] = [
    ("火球分析", "競合分析", "環境分析とか火球分析もだからワークシートは"),
    ("姿勢のに提供している", "資生堂に提供している", "姿勢のに提供しているあの研修ですね"),
]

# wrong が実在語 → 検出ヒントとしてのみ蓄積（誤爆防止）
CONTEXT_PAIRS: list[tuple[str, str, str]] = [
    ("最小項数", "最小工数", "多分最小項数でご提案できるところだし"),
    ("累計化", "類型化", "阻害要因はこれとこれとこれです、って累計化がある気がして"),
    ("漏らされてる", "網羅されてる", "あそこは今もう全部漏らされてるわけではない"),
    ("裁き", "裁量", "完全に自分の裁きとしてやられてる"),
    ("GPDC", "G-PDCA", "プロマネとして段取り、いわゆるGPDCもあるんですけど"),
    ("新卒ブロック", "新卒プロパー", "新卒ブロックとか未経験の異動の方など"),
]


def main() -> int:
    added = updated = skipped = 0
    for pairs, scope in ((GLOBAL_PAIRS, "global"), (CONTEXT_PAIRS, "context")):
        for wrong, right, example in pairs:
            r = add_learned_correction(
                wrong=wrong, right=right, via=VIA, job_id=JOB_ID,
                example=example, confidence="high", scope=scope,
            )
            action = r.get("action")
            if action == "added":
                added += 1
            elif action == "updated":
                updated += 1
            else:
                skipped += 1
                print(f"  skipped[{scope}]: {wrong!r} -> {right!r} ({r.get('reason')})")
    print(f"learned: added={added} updated={updated} skipped={skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
