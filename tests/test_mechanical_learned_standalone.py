"""Tests for learned dict standalone + honorific whitelist gates."""
from __future__ import annotations

import unittest

from mechanical_correct_text import (
    apply_dictionary_replacements_standalone,
    filter_learned_for_mechanical_apply,
)


class MechanicalLearnedGateTests(unittest.TestCase):
    def test_standalone_avoids_partial_match(self) -> None:
        learned = {"投手": "通り"}
        text = "全面投手の話"
        out = apply_dictionary_replacements_standalone(text, learned)
        self.assertEqual(out, "全面投手の話")

    def test_standalone_replaces_isolated_word(self) -> None:
        learned = {"投手": "通り"}
        text = "投手の話。全面投手も。"
        out = apply_dictionary_replacements_standalone(text, learned)
        self.assertIn("通りの話", out)
        self.assertIn("全面投手", out)

    def test_colloquial_san_skipped(self) -> None:
        learned = {"エビさん": "海老様"}
        filtered = filter_learned_for_mechanical_apply(
            learned, participants=["海老様", "森川様"]
        )
        self.assertEqual(filtered, {})

    def test_formal_honorific_requires_participant_right(self) -> None:
        learned = {"恵比寿様": "海老様", "全面投手": "全面通り"}
        filtered = filter_learned_for_mechanical_apply(
            learned, participants=["海老様", "森川様"]
        )
        self.assertEqual(filtered.get("恵比寿様"), "海老様")
        self.assertEqual(filtered.get("全面投手"), "全面通り")


if __name__ == "__main__":
    unittest.main()
