"""Tests for Phase 10 edit proposal schema and fact guards."""
from __future__ import annotations

import unittest

from edit_proposal_schema import (
    FACT_NUMERIC,
    FACT_PROPER_NOUN,
    FACT_FILLER_GARBLE,
    VERDICT_ASK_WITH_CANDIDATE,
    VERDICT_ASK_WITHOUT_CANDIDATE,
    VERDICT_AUTO_CORRECT,
    VERDICT_AUTO_DELETE,
    enforce_fact_routing,
    to_unknown_point,
)
from fact_classify import classify_fact_class, reclassify_proposal
from fact_integrity_gate import simulate_apply_proposals, verify_fact_integrity


class FactRoutingTests(unittest.TestCase):
    def test_numeric_blocks_auto_correct(self) -> None:
        p = enforce_fact_routing(
            {
                "verdict": VERDICT_AUTO_CORRECT,
                "fact_class": FACT_NUMERIC,
                "hypothesis": "",
                "span_before": "85万円",
            }
        )
        self.assertEqual(p["original_verdict"], VERDICT_AUTO_CORRECT)
        self.assertEqual(p["verdict"], VERDICT_ASK_WITHOUT_CANDIDATE)

    def test_proper_noun_with_hypothesis_becomes_ask_with_candidate(self) -> None:
        p = enforce_fact_routing(
            {
                "verdict": VERDICT_AUTO_CORRECT,
                "fact_class": FACT_PROPER_NOUN,
                "hypothesis": "盛岡",
                "span_before": "義理をか",
            }
        )
        self.assertEqual(p["verdict"], VERDICT_ASK_WITH_CANDIDATE)
        self.assertEqual(p["routing_override"], "fact_class_guard")

    def test_filler_garble_allows_auto_delete(self) -> None:
        p = enforce_fact_routing(
            {
                "verdict": VERDICT_AUTO_DELETE,
                "fact_class": "filler_garble",
                "span_before": "16時にちょっという",
            }
        )
        self.assertEqual(p["verdict"], VERDICT_AUTO_DELETE)


class FactClassifyTests(unittest.TestCase):
    def test_code_detects_numeric(self) -> None:
        fc, src = classify_fact_class(span_before="予算は85万円", llm_fact_class="lexical_fluency")
        self.assertEqual(fc, FACT_NUMERIC)
        self.assertEqual(src, "code_override")

    def test_garble_digit_not_numeric(self) -> None:
        fc, src = classify_fact_class(
            span_before="ちょっと 16 時にちょっという",
            llm_fact_class="filler_garble",
            llm_verdict="auto_delete",
        )
        self.assertEqual(fc, FACT_FILLER_GARBLE)
        self.assertNotEqual(fc, FACT_NUMERIC)

    def test_garble_llm_numeric_overridden(self) -> None:
        fc, _ = classify_fact_class(
            span_before="16 時にちょっという",
            llm_fact_class="numeric",
            llm_verdict="ask_without_candidate",
        )
        self.assertEqual(fc, FACT_FILLER_GARBLE)

    def test_participant_is_proper_noun(self) -> None:
        fc, _ = classify_fact_class(
            span_before="相原さんの件",
            llm_fact_class="lexical_fluency",
            meeting_profile={"participants": ["相原"]},
        )
        self.assertEqual(fc, FACT_PROPER_NOUN)


class FactIntegrityGateTests(unittest.TestCase):
    def test_numbers_must_persist(self) -> None:
        before = "予算85万円と75万円の話。"
        after = "予算の話。"
        result = verify_fact_integrity(before, after)
        self.assertFalse(result.ok)
        self.assertTrue(any("numbers_missing" in v for v in result.violations))

    def test_simulate_delete_preserves_numbers_elsewhere(self) -> None:
        before = "85万円。あのーちょっと。横浜へ。"
        proposals = [
            {
                "verdict": VERDICT_AUTO_DELETE,
                "span_before": "あのーちょっと",
                "span_start": before.find("あのーちょっと"),
            }
        ]
        after = simulate_apply_proposals(before, proposals)
        result = verify_fact_integrity(before, after)
        self.assertTrue(result.ok)


class UnknownPointDeriveTests(unittest.TestCase):
    def test_to_unknown_point_ask_without_candidate(self) -> None:
        up = to_unknown_point(
            {
                "proposal_id": "x",
                "verdict": VERDICT_ASK_WITHOUT_CANDIDATE,
                "span_before": "義理をか",
                "anomaly_word": "義理をか",
                "evidence": "八戸から義理をか",
                "importance": "地名文脈",
            }
        )
        self.assertEqual(up["source"], "contextual_editor")
        self.assertEqual(up["question_kind"], "without_candidate")


if __name__ == "__main__":
    unittest.main()
