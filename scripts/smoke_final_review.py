#!/usr/bin/env python3
"""最終批評パスの実 API スモークテスト（NREPT案件の症状を再現した合成テキスト）。"""
from __future__ import annotations

import io
import os
import sys
import tempfile
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from repo_env import load_dotenv_local  # noqa: E402

load_dotenv_local()
os.environ["FINAL_REVIEW_MODE"] = "shadow"

from final_review_pass import run_final_review  # noqa: E402

SAMPLE = """▼グループ合同7年次研修と自社8年次問題解決の並行実施方針

グループの方で7年次を対象とした問題解決をやるんですが、当社の場合って8年時を対象に問題解決やってるんですね。グループとして7年次以上を対象とした問題解決研修、はいで当社として8年時を対象とした問題解決研修をやります。ただ来年度になると今年グループの7年次研修受けた方は8年時になっちゃうので、来年度のL2問題、うん、実施をしない方向、はいに考えています。

うん、A1が4クラス80名、はいで、L2が5クラスで109名です。

あんまり見と一緒でフレームアップめっちゃいます。こう応用版みたいな、そうだね。

もしよかったらそういう研修をトライアルで今実施させていただいてたりするので、無料でそういうのも、よかったらちょっとご体感いただくといいかもしれないです。ぜひ！2点合えばちょっと見てみたいなと。

最後じゃあ濡れないように傘を刺そうっていう比喩なんですけど。
"""


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        _, report = run_final_review(job_dir=tmp, text=SAMPLE)
        print(f"mode={report['mode']} model={report['model']}")
        if report.get("error"):
            print(f"error={report['error']}")
            return 1
        for f in report["findings"]:
            print(
                f"[{f.get('type')}/{f.get('confidence')}] "
                f"quote={str(f.get('quote', ''))[:50]!r}"
            )
            print(f"   issue: {str(f.get('issue', ''))[:90]}")
            print(f"   fix:   {str(f.get('fix', ''))[:60]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
