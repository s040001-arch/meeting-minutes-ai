"""修正ペアへの質問テンプレート断片の混入を防ぐ決定論サニタイザ。

2026-08-06 楽天ジョブ(072038)で、番号付き質問（「1.… 2.…」形式）への
回答から生成された修正ペアの correct 側に「2.」というテンプレ断片が
混入し、本文に「…してもらうとか 2.こういう…」と書き込まれた。
回答抽出LLMの出力は本文へ直接入るため、適用直前に決定論的に浄化する。

方針:
- 「2.」「10.」等の列挙マーカーは、wrong（置換元）に同じマーカーが
  存在する場合のみ本文由来とみなして残す。それ以外は除去。
- 小数（52.5）や日付表記は正規表現の前後条件で保護する。
- ①②等の丸数字も同様に扱う。
"""
from __future__ import annotations

import re

# 「2.」「10.」のような列挙マーカー。
# 前が数字/./, なら小数・節番号の一部なので対象外。後ろに数字が続く場合
# （52.5 等）も対象外。前後の空白ごと除去して隙間を残さない。
_ENUM_RE = re.compile(r"[ \t\u3000]*(?<![0-9.,])[0-9]{1,2}\.(?![0-9])[ \t\u3000]*")
_CIRCLED_RE = re.compile(r"[ \t\u3000]*[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮][ \t\u3000]*")


def sanitize_correction_text(correct: str, *, wrong: str = "") -> str:
    """correct から wrong に存在しない列挙マーカーを除去して返す。"""
    if not correct:
        return correct
    wrong = wrong or ""

    def repl(m: re.Match) -> str:
        token = m.group(0).strip()
        # 置換元にも同じマーカーがある → 本文由来なので保持
        if token and token in wrong:
            return m.group(0)
        return ""

    out = _ENUM_RE.sub(repl, correct)
    out = _CIRCLED_RE.sub(repl, out)
    return out
