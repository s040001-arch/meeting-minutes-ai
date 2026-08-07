from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fact_token_audit import (
    audit_fact_token_diff,
    protected_token_multisets,
)


class ProtectedTokenTests(unittest.TestCase):
    def test_single_digit_and_ordinal(self) -> None:
        numbers, kanji, names = protected_token_multisets(
            "第2希望で、1対1にするか。七、八割です。山屋さんと相原様。"
        )
        self.assertIn("2", numbers)
        self.assertEqual(numbers["1"], 2)
        self.assertIn("八割", kanji)
        self.assertIn("山屋さん", names)
        self.assertIn("相原様", names)

    def test_kanji_dai_pattern(self) -> None:
        _, kanji, _ = protected_token_multisets("第一志望と三ヶ月の研修")
        self.assertIn("第一", kanji)
        self.assertIn("三ヶ月", kanji)


class SentinelTests(unittest.TestCase):
    def test_detects_ordinal_swap(self) -> None:
        # 実害の再現: 第2希望→第1希望（dense repair がすり抜けたケース）
        before = "大体七、八割が第2希望のやつが来てるんで前向きだね"
        after = "大体七、八割が第1希望のところに来ているんで前向きだね"
        violations = audit_fact_token_diff(before, after)
        tokens = {v["token"] for v in violations}
        self.assertIn("第2", tokens)
        self.assertIn("第1", tokens)

    def test_detects_kanji_number_loss(self) -> None:
        violations = audit_fact_token_diff(
            "研修は三ヶ月で計画します", "研修で計画します"
        )
        self.assertTrue(any(v["token"] == "三ヶ月" for v in violations))

    def test_confirmed_pair_is_allowed(self) -> None:
        # ユーザー確認済みの修正（山谷さん→山屋さん）は違反にしない
        violations = audit_fact_token_diff(
            "山谷さんに確認します",
            "山屋さんに確認します",
            allow_pairs=[{"wrong": "山谷さん", "correct": "山屋さん"}],
        )
        self.assertEqual(violations, [])

    def test_clean_smoothing_passes(self) -> None:
        violations = audit_fact_token_diff(
            "えっと、あの、8月10日の15時半で、はい、お願いします",
            "8月10日の15時半でお願いします。",
        )
        self.assertEqual(violations, [])

    def test_violation_has_context_quote(self) -> None:
        violations = audit_fact_token_diff(
            "価格は75万円です", "価格は70万円です"
        )
        self.assertTrue(violations)
        for v in violations:
            self.assertTrue(v["quote"])


class SectionConsistencyTests(unittest.TestCase):
    def _fake_response(self, rows: list[dict]) -> SimpleNamespace:
        return SimpleNamespace(
            content=[SimpleNamespace(text=json.dumps(rows, ensure_ascii=False))]
        )

    def test_demotes_tentative_decision(self) -> None:
        from section_consistency_check import check_and_fix_sections

        sections = {
            "decisions": [
                "3対3のN対N型メンター制で進める",
                "8月10日15時半にレビューを実施する",
            ],
            "open_issues": ["メンターの年次は検討中"],
        }
        client = MagicMock()
        client.messages.create.return_value = self._fake_response(
            [
                {"index": 0, "evidence": "tentative", "quote": "いいかもね", "conflict": None},
                {"index": 1, "evidence": "explicit", "quote": "じゃあ15時半で", "conflict": None},
            ]
        )
        with patch("anthropic.Anthropic", return_value=client), patch.dict(
            "os.environ", {"ANTHROPIC_API_KEY": "test"}
        ):
            fixed, report = check_and_fix_sections(sections, "発言録テキスト")
        self.assertEqual(len(fixed["decisions"]), 1)
        self.assertIn("8月10日15時半", fixed["decisions"][0])
        self.assertEqual(len(fixed["open_issues"]), 2)
        self.assertIn("有力案の段階", fixed["open_issues"][1])
        self.assertEqual(len(report["demoted"]), 1)

    def test_fail_open_on_llm_error(self) -> None:
        from section_consistency_check import check_and_fix_sections

        sections = {"decisions": ["何かの決定"], "open_issues": []}
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("boom")
        with patch("anthropic.Anthropic", return_value=client), patch.dict(
            "os.environ", {"ANTHROPIC_API_KEY": "test"}
        ):
            fixed, report = check_and_fix_sections(sections, "text")
        self.assertEqual(fixed["decisions"], ["何かの決定"])
        self.assertIn("consistency_llm_failed", report["error"])


if __name__ == "__main__":
    unittest.main()
