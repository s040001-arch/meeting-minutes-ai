"""Tests for Phase 10 step-3 ③ prioritization and bundling."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from question_bundle_step3 import (
    TIER_FACT_ERROR,
    TIER_LOW,
    TIER_MATERIAL,
    bundle_safe_step3_items,
    prioritize_step3_items,
    score_step3_materiality,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "scripts" / "fixtures" / "phase10_step3_164142_answers_template.json"


class Step3MaterialityTests(unittest.TestCase):
    def test_8suji_tier_a(self) -> None:
        meta = score_step3_materiality(
            {
                "anomaly_word": "8 数字",
                "span_before": "展開をするっていうのが 8 数字なんじゃないか",
                "context": "グループ企業に展開をするっていうのが 8 数字なんじゃないか",
                "fact_class": "filler_garble",
                "reason": "「8数字」が文脈に合わず候補不明",
            }
        )
        self.assertEqual(meta["priority_tier"], TIER_FACT_ERROR)

    def test_1000sha_tier_a(self) -> None:
        meta = score_step3_materiality(
            {
                "anomaly_word": "小林さん",
                "span_before": "イオンでも 1000 車ぐらい小林さんですね",
                "context": "1500 社とか 2000 近いです。イオンでも 1000 車ぐらい",
                "fact_class": "uncertain",
                "reason": "「1000車」が崩れ",
            }
        )
        self.assertEqual(meta["priority_tier"], TIER_FACT_ERROR)

    def test_filler_garble_tier_c(self) -> None:
        meta = score_step3_materiality(
            {
                "anomaly_word": "ぜつって",
                "span_before": "ぜつって",
                "context": "子会社の社長を引っ張り出して",
                "fact_class": "filler_garble",
                "reason": "口語崩れ",
            }
        )
        self.assertEqual(meta["priority_tier"], TIER_LOW)


class Step3Export164142Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not FIXTURE.is_file():
            raise unittest.SkipTest(f"missing {FIXTURE}")
        doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.meta = doc.get("meta") or {}
        cls.answers = list(doc.get("answers") or [])

    def test_count_sixty_six_raw(self) -> None:
        # Fixture was last (legitimately) regenerated with count_raw=67 — the ③
        # raw count grew by one ask_without_candidate item after this fixture's
        # 8 already-answered items were recorded. Pin to the fixture's actual
        # value rather than regenerating it (regenerating would wipe the
        # recorded answer_text values).
        self.assertEqual(self.meta.get("count_raw"), 67)

    def test_tier_a_first(self) -> None:
        first = self.answers[0]
        self.assertEqual(first.get("priority_tier"), TIER_FACT_ERROR)
        self.assertIn("8 数字", str(first.get("anomaly_word")))
        self.assertLessEqual(int(first.get("priority_rank") or 99), 3)

    def test_mitsubishi_bundle_if_present(self) -> None:
        mitsu = [a for a in self.answers if a.get("anomaly_word") == "三菱商事"]
        if len(mitsu) == 1 and mitsu[0].get("targets"):
            self.assertEqual(len(mitsu[0]["targets"]), 2)


class Step3BundleUnitTests(unittest.TestCase):
    def test_same_anomaly_word_bundles(self) -> None:
        items = prioritize_step3_items(
            [
                {
                    "question_id": "a",
                    "anomaly_word": "三菱商事",
                    "span_before": "span1",
                    "span_start": 100,
                    "fact_class": "proper_noun",
                    "context": "ctx1",
                    "selected_unknown": {"verdict": "ask_without_candidate"},
                },
                {
                    "question_id": "b",
                    "anomaly_word": "三菱商事",
                    "span_before": "span2",
                    "span_start": 200,
                    "fact_class": "proper_noun",
                    "context": "ctx2",
                    "selected_unknown": {"verdict": "ask_without_candidate"},
                },
            ]
        )
        bundled = bundle_safe_step3_items(items)
        self.assertEqual(len(bundled), 1)
        self.assertEqual(len(bundled[0]["targets"]), 2)


if __name__ == "__main__":
    unittest.main()
