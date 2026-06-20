"""Tests for ③ anomaly anchor repair and 試合 bundling."""
from __future__ import annotations

import unittest

from pinpoint_answer_apply import apply_answers
from question_bundle_step3 import bundle_safe_step3_items, prioritize_step3_items
from step3_anomaly_repair import (
    audit_anomaly_span_alignment,
    detect_numeric_sha_unit_mismatch,
    detect_shiai_company_garble,
    expand_shiai_step3_items,
    repair_step3_anomaly_anchor,
    shiai_pinpoint_item,
)


class NumericShaRepairTests(unittest.TestCase):
    def test_detect_1000sha(self) -> None:
        sb = "イオンでも 1000 車ぐらい小林さんですね"
        ctx = "1500 社とか 2000 近いです。イオンでも 1000 車ぐらい"
        self.assertEqual(detect_numeric_sha_unit_mismatch(sb, ctx), "1000 車")

    def test_repair_misanchor_kobayashi(self) -> None:
        item = {
            "anomaly_word": "小林さん",
            "span_before": "イオンでも 1000 車ぐらい小林さんですね",
            "context": "1500 社とか 2000 近いです。",
            "fact_class": "uncertain",
        }
        out = repair_step3_anomaly_anchor(item)
        self.assertEqual(out["anomaly_word"], "1000 車")
        self.assertEqual(out["fact_class"], "numeric")
        self.assertIn("numeric_unit_sha", out["anomaly_repair"][0])

    def test_idempotent(self) -> None:
        item = {
            "anomaly_word": "1000 車",
            "span_before": "イオンでも 1000 車ぐらい混ざってですね",
            "context": "1500 社とか",
            "fact_class": "numeric",
        }
        out = repair_step3_anomaly_anchor(item)
        self.assertNotIn("anomaly_repair", out)

    def test_audit_clean_after_repair(self) -> None:
        item = repair_step3_anomaly_anchor(
            {
                "anomaly_word": "小林さん",
                "span_before": "イオンでも 1000 車ぐらい小林さんですね",
                "context": "700 社ぐらい子会社",
            }
        )
        self.assertEqual(audit_anomaly_span_alignment(item), [])


class ShiaiBundleTests(unittest.TestCase):
    def test_detect_shiai_contexts(self) -> None:
        self.assertTrue(
            detect_shiai_company_garble(
                "200 試合やり 200 人分データ",
                "4 社の例がある",
            )
        )
        self.assertTrue(
            detect_shiai_company_garble(
                "CCI が 2 試合やったあと小松放棄もやりましたね",
                "松コープと矢崎",
            )
        )

    def test_expand_splits_komatsu_shiai(self) -> None:
        raw = [
            {
                "question_id": "a",
                "anomaly_word": "200 試合",
                "span_before": "200 試合やり 200 人分データ",
                "context": "4 社の例",
                "span_start": 100,
                "fact_class": "numeric",
                "selected_unknown": {"verdict": "ask_without_candidate"},
            },
            {
                "question_id": "b",
                "anomaly_word": "小松放棄",
                "span_before": "CCI が 2 試合やったあと小松放棄もやりましたね",
                "context": "松コープと矢崎",
                "span_start": 50,
                "fact_class": "uncertain",
                "selected_unknown": {"verdict": "ask_without_candidate"},
            },
        ]
        expanded = expand_shiai_step3_items(raw)
        self.assertEqual(len(expanded), 3)
        shiai = [x for x in expanded if x.get("garble_pattern")]
        self.assertEqual(len(shiai), 2)
        self.assertTrue(all(x["anomaly_word"] == "試合" for x in shiai))

    def test_bundle_one_answer_sha(self) -> None:
        expanded = expand_shiai_step3_items(
            [
                {
                    "question_id": "a",
                    "anomaly_word": "200 試合",
                    "span_before": "200 試合やり 200 人分データ",
                    "context": "4 社",
                    "span_start": 100,
                    "fact_class": "numeric",
                    "selected_unknown": {"verdict": "ask_without_candidate"},
                },
                {
                    "question_id": "b",
                    "anomaly_word": "小松放棄",
                    "span_before": "CCI が 2 試合やったあと小松放棄もやりましたね",
                    "context": "CCI 松コープ",
                    "span_start": 50,
                    "fact_class": "uncertain",
                    "selected_unknown": {"verdict": "ask_without_candidate"},
                },
            ]
        )
        bundled = bundle_safe_step3_items(prioritize_step3_items(expanded))
        shiai_bundle = [b for b in bundled if b.get("garble_pattern") == "shiai_to_sha" and b.get("targets")]
        self.assertEqual(len(shiai_bundle), 1)
        self.assertEqual(len(shiai_bundle[0]["targets"]), 2)
        text = (
            "まばらまいて 200 試合やり 200 人分データ出てきまし。"
            "CCI が 2 試合やったあと小松放棄もやりましたね。"
        )
        out, applied = apply_answers(
            text,
            [{**shiai_bundle[0], "answer_text": "社"}],
        )
        self.assertIn("200 社やり", out)
        self.assertIn("2 社やった", out)
        self.assertEqual(len([a for a in applied if not a.get("error")]), 2)

    def test_shiai_and_komatsu_same_span_apply_order(self) -> None:
        text = "CCI が 2 試合やったあと小松放棄もやりましたね。"
        shiai = {
            "question_id": "bundle",
            "garble_pattern": "shiai_to_sha",
            "answer_text": "社",
            "anomaly_word": "試合",
            "targets": [
                {
                    "anomaly_word": "試合",
                    "span_before": "CCI が 2 試合やったあと小松放棄もやりましたね",
                    "span_start": 0,
                }
            ],
        }
        komatsu = {
            "question_id": "komatsu",
            "review_index": 7,
            "answer_text": "小松鋼機",
            "anomaly_word": "小松放棄",
            "span_before": "CCI が 2 試合やったあと小松放棄もやりましたね",
            "span_start": 0,
        }
        out, applied = apply_answers(text, [shiai, komatsu])
        self.assertIn("CCI が 2 社やったあと小松鋼機もやりましたね", out)
        self.assertEqual(len([a for a in applied if a.get("error")]), 0)


if __name__ == "__main__":
    unittest.main()
