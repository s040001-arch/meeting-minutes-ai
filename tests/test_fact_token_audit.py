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

    def test_incomplete_rows_fail_open_not_explicit(self) -> None:
        # GPT監査#4: LLM応答に欠けたindexを無言でexplicit扱いしない
        from section_consistency_check import check_and_fix_sections

        sections = {
            "decisions": ["決定A", "決定B", "決定C"],
            "open_issues": [],
        }
        client = MagicMock()
        client.messages.create.return_value = self._fake_response(
            [{"index": 0, "evidence": "explicit", "quote": "", "conflict": None}]
        )
        with patch("anthropic.Anthropic", return_value=client), patch.dict(
            "os.environ", {"ANTHROPIC_API_KEY": "test"}
        ):
            fixed, report = check_and_fix_sections(sections, "text")
        self.assertEqual(fixed["decisions"], ["決定A", "決定B", "決定C"])
        self.assertIn("consistency_incomplete_rows", report["error"])

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


class ConfirmedPairGuardTests(unittest.TestCase):
    def test_enum_marker_wrong_rejected(self) -> None:
        # GPT監査#5: 「2.」等の列挙マーカーは全文強制置換の対象にしない
        from confirmed_corrections import _pair_is_safe

        self.assertFalse(_pair_is_safe("2.", "2. こういう"))
        self.assertFalse(_pair_is_safe("３）", "何か"))
        self.assertFalse(_pair_is_safe("12", "13"))
        self.assertTrue(_pair_is_safe("湯でみ", "Udemy"))
        self.assertTrue(_pair_is_safe("山谷さん", "山屋さん"))


class ScopeGatedReplacementTests(unittest.TestCase):
    """API がある本番では scope 判定で実在語の誤爆を防ぐ。
    ここでは decide_scope をモックし、その分岐を検証する。"""

    def test_garble_replaces_all_occurrences(self) -> None:
        from recognition_batch import apply_batch_corrections

        body = "湯でみの話。次も湯でみを使う。"
        parsed = [{"anomaly_id": "a1", "word": "湯でみ", "action": "correct", "correction": "Udemy"}]
        with patch(
            "learned_corrections_store.decide_scope", return_value="global"
        ):
            out, applied = apply_batch_corrections(
                body, parsed, api_key="k"
            )
        self.assertNotIn("湯でみ", out)
        self.assertEqual(out.count("Udemy"), 2)

    def test_real_word_untagged_not_replaced(self) -> None:
        # 実在語（scope=context）はタグ付き箇所のみ直し、別文脈の同語は残す
        from recognition_batch import apply_batch_corrections, VERIFY_TAG

        body = f"部署の移動{VERIFY_TAG}があった。机を移動した。"
        parsed = [{"anomaly_id": "a1", "word": "移動", "action": "correct", "correction": "異動"}]
        with patch(
            "learned_corrections_store.decide_scope", return_value="context"
        ):
            out, applied = apply_batch_corrections(
                body, parsed, api_key="k"
            )
        self.assertIn("部署の異動があった", out)
        self.assertIn("机を移動した", out)  # 物理移動は保護

    def test_no_api_key_replaces_globally(self) -> None:
        # API 無し（テスト・オフライン）は従来どおり全置換
        from recognition_batch import apply_batch_corrections

        body = "湯でみの話。次も湯でみを使う。"
        parsed = [{"anomaly_id": "a1", "word": "湯でみ", "action": "correct", "correction": "Udemy"}]
        out, applied = apply_batch_corrections(body, parsed)
        self.assertEqual(out.count("Udemy"), 2)


class HeadingValidationTests(unittest.TestCase):
    def test_heading_with_unsupported_number_falls_back(self) -> None:
        # GPT監査#3: 本文にない数値を含む見出しは中立見出しへ
        from transcript_section_summarizer import _validate_heading_tokens

        result = _validate_heading_tokens(
            "59歳対象層への施策", "対象は57歳の層です。施策を検討した。", 3
        )
        self.assertEqual(result, "")

    def test_heading_kanji_digit_normalization_passes(self) -> None:
        from transcript_section_summarizer import _validate_heading_tokens

        result = _validate_heading_tokens(
            "7-8割が第1〜第2希望配属",
            "大体七、八割が第1希望から第2希望のところに来ている",
            1,
        )
        self.assertEqual(result, "7-8割が第1〜第2希望配属")

    def test_heading_name_check(self) -> None:
        from transcript_section_summarizer import _validate_heading_tokens

        result = _validate_heading_tokens(
            "田中様の提案説明", "山屋様が提案を説明した。", 2
        )
        self.assertEqual(result, "")
        result2 = _validate_heading_tokens(
            "山屋様の提案説明", "山屋さんが提案を説明した。", 2
        )
        self.assertEqual(result2, "山屋様の提案説明")


if __name__ == "__main__":
    unittest.main()
