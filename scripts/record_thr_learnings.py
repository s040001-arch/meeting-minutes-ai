#!/usr/bin/env python3
"""thrジョブで確定した修正を学習辞書へ記録する（チャット修正分の還元）。

- GLOBAL_PAIRS: wrong が実在語として現れない崩れ表記 → 次ジョブから機械補正で自動置換
- CONTEXT_PAIRS: wrong が実在語（本数/本社など）→ 盲目置換せず、
  coherence 検出プロンプトのヒントとして蓄積（文脈が合えば候補付きで検出→質問）
- 追加で、高確度の取り残し1件（テーマ→手間）を after_qa に直接適用する。
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from learned_corrections_store import add_learned_correction  # noqa: E402

JOB_ID = "job_20260705_055804_2026_0624_thr社_運営改善_西脇様_竹中様_福田_相原"
VIA = "chat_fix"

# wrong が崩れ表記でしか現れない → 機械補正で無条件置換してよい
GLOBAL_PAIRS: list[tuple[str, str, str]] = [
    ("自施要項", "実施要項", "その実施をこう自施要項って言い方が"),
    ("就高年収", "集合研修", "研修って本当にあの就高年収しかなく"),
    ("オブザード", "オブザーバー", "オブザードに誰がいるのか"),
    ("講師ング", "コーチング", "キャリアコンサルティングとか講師ングみたいな形"),
    ("近距離連絡先", "緊急連絡先", "オブザーブ近距離連絡先ってこの辺り"),
    ("研究連絡先", "緊急連絡先", "事務局担当者と研究連絡先だったりとか"),
    ("常駐大", "常駐型", "別にあの常駐大じゃなくて"),
    ("本末転という", "本末転倒という", "新たな作業が発生するっていうのは本末転という"),
]

# wrong が実在語 → 検出ヒントとしてのみ蓄積（誤爆防止）
CONTEXT_PAIRS: list[tuple[str, str, str]] = [
    ("本数", "工数", "工数がさっき削減って言いましたけど（作業量文脈の本数）"),
    ("コス", "工数", "1 つ 1 つのまコス自体がすごく膨大"),
    ("項数", "工数", "御社における項数削減"),
    ("交通", "工数", "色々あの交通削減できたらいいな（削減文脈）"),
    ("コース", "工数", "事務局全体的にコースがあ増えそうだな（負担文脈）"),
    ("実証校", "実施要項", "ま実証校についてはそんなに工数として"),
    ("実施をこう", "実施要項", "NESTの実施をこう見に行かれる"),
    ("恩師", "御社", "こう恩師のその事務局の工数の削減"),
    ("王者", "御社", "かつはい王者として手間がない"),
    ("お社", "御社", "お社の中でのっていうところ"),
    ("音叉", "御社", "その辺りの扱いが音叉の中で"),
    ("今週", "御社", "そのあたりのま今週の中での活用方法"),
    ("聖書", "弊社", "聖書の場合では 1度も時間変更に（話者立場）"),
    ("車内", "社内", "今度は逆に車内の手間みたいなの"),
    ("祖母", "齟齬", "認識の祖母がないできるようにしたい"),
    ("ベッド", "別途", "っていうのはまたベッドやってますけど"),
    ("ペット", "別途", "そちらはペット私ができるかな"),
    ("教育機関", "教育期間", "覚えるまでに教育機関というか"),
    ("研修室", "研修数", "クールが増えてったり、研修室が増えることで"),
    ("面倒さん", "ベンダーさん", "いろんなコンサル含めて面倒さんにこうお伺い"),
    ("ミスト", "NEST", "ミストに関するあのものことで（システム名文脈）"),
    ("ウエスト", "NEST", "あの今そのウエスト上にない情報（システム名文脈）"),
]

# 高確度の取り残し（直接適用）
DIRECT_FIXES: list[tuple[str, str]] = [
    ("そのテーマをちょっと省いていけたらな", "その手間をちょっと省いていけたらな"),
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

    path = REPO / "data" / "transcriptions" / JOB_ID / "merged_transcript_after_qa.txt"
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        n = 0
        for old, new in DIRECT_FIXES:
            c = text.count(old)
            if c:
                text = text.replace(old, new)
                n += c
                print(f"direct_fix x{c}: {old[:30]!r}")
            else:
                print(f"direct_fix NOT_FOUND: {old[:40]!r}")
        if n:
            path.write_text(text, encoding="utf-8")
            print(f"after_qa_saved direct_fixes={n}")
    else:
        print(f"after_qa missing (skip direct fixes): {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
