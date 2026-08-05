"""影響度ベースの質問クラスタリング（question_impact.py）のテスト。"""

from __future__ import annotations

import unittest

from question_impact import cluster_pending_findings


def _point(word: str, pos: int, anomaly_id: str = "") -> dict:
    return {
        "anomaly_id": anomaly_id or f"id_{word}_{pos}",
        "anomaly_word": word,
        "text": word,
        "context_position_in_transcript": pos,
        "status": "open",
    }


class ClusterPendingFindingsTests(unittest.TestCase):
    def test_variants_of_same_entity_are_clustered(self) -> None:
        # シュニア/シニア のような表記ゆれは同じ答えで解決する一群
        points = [
            _point("シュニア", 10),
            _point("シニアの担当", 50),
            _point("決裁フロー", 100),
        ]
        clusters = cluster_pending_findings(points, full_text="")
        sizes = sorted(len(c["items"]) for c in clusters)
        self.assertEqual(sizes, [1, 2])

    def test_honorific_variants_are_clustered(self) -> None:
        points = [_point("山谷さん", 10), _point("山谷様", 200)]
        clusters = cluster_pending_findings(points, full_text="")
        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(clusters[0]["items"]), 2)

    def test_frequent_token_has_higher_impact(self) -> None:
        # 本文に何度も出る語ほど、答えの波及が大きい=先に聞く
        full_text = "アジャイル研修の話。アジャイル導入。アジャイル推進。単発の誤字。"
        points = [
            _point("単発の誤字", 30),
            _point("アジャイル", 0),
        ]
        clusters = cluster_pending_findings(points, full_text=full_text)
        self.assertEqual(clusters[0]["items"][0]["anomaly_word"], "アジャイル")
        self.assertGreater(clusters[0]["score"], clusters[1]["score"])

    def test_long_quotes_cluster_by_salient_token(self) -> None:
        # 文レベルの引用同士でも、核となる語（人名など）が共通なら一群
        points = [
            _point("ヤマヤさんが担当すると言った件です", 10),
            _point("その後のヤマヤの発言は不明瞭でした", 400),
            _point("完全に無関係な崩れフレーズ", 200),
        ]
        clusters = cluster_pending_findings(points, full_text="")
        top_two = sorted(len(c["items"]) for c in clusters)
        self.assertIn(2, top_two)

    def test_tie_break_prefers_earlier_position(self) -> None:
        # 同点なら文書の前方から（序盤の回答ほどカスケードが遠くまで効く）
        points = [_point("後半の崩れ", 900), _point("前半の崩れ", 10)]
        clusters = cluster_pending_findings(points, full_text="")
        self.assertEqual(clusters[0]["first_pos"], 10)


if __name__ == "__main__":
    unittest.main()
