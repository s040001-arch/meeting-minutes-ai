"""Tests for reconstruct_span (Phase 10.3): code-enforced span reconstruction.

The three guard-rejection tests correspond to the failure modes that made the
previous free-form "rewrite ±1 sentence" approach unsafe: span overreach
(length_guard), fact drift (fact_gate), and meaning drift undetected by
length/fact checks alone (semantic_gate).
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from reconstruct_span import (
    SpanReconstructResult,
    apply_span_reconstruction,
    reconstruct_span,
)


class ReconstructSpanGuardTests(unittest.TestCase):
    def test_no_api_key_fails_closed(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            result = reconstruct_span(
                span_target="記号を取ってる",
                confirmed_info="参加は任意の形式で良い。",
                api_key="",
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "llm_call")

    @patch("reconstruct_span._call_llm_reconstruct")
    def test_length_guard_rejects_span_overreach(self, mock_call) -> None:
        # Revert case 1: model rewrites far beyond the target span instead of
        # the minimal in-place edit — this is exactly the overreach observed
        # in the original Opus ±1-sentence prompting.
        span = "記号を取ってる"
        mock_call.return_value = (
            '{"replacement": "'
            + "参加は任意の形式で良いという話です。" * 5
            + '"}'
        )
        result = reconstruct_span(
            span_target=span,
            confirmed_info="参加は任意の形式で良い。",
            api_key="test-key",
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "length_guard")

    @patch("reconstruct_span._call_llm_reconstruct")
    def test_fact_gate_rejects_number_drift(self, mock_call) -> None:
        # Revert case 2: replacement silently drops a confirmed numeric fact.
        mock_call.return_value = '{"replacement": "承認通過率がおよそ半分です。"}'
        result = reconstruct_span(
            span_target="やっぱり1/3ぐらい。",
            confirmed_info="合瀬さん部門の承認通過率が1/3。",
            api_key="test-key",
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "fact_gate")
        self.assertIn("numbers_missing", result.reason)

    @patch("reconstruct_span._call_llm_reconstruct")
    def test_fact_gate_rejects_protected_name_drift(self, mock_call) -> None:
        mock_call.return_value = '{"replacement": "田中さんが部長です。"}'
        result = reconstruct_span(
            span_target="鷹股さんが部長です。",
            confirmed_info="部長は鷹股さん。",
            protected_names=["鷹股さん"],
            api_key="test-key",
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "fact_gate")
        self.assertIn("names_missing", result.reason)

    @patch("reconstruct_span._call_llm_semantic_ok")
    @patch("reconstruct_span._call_llm_reconstruct")
    def test_semantic_gate_rejects_meaning_change(self, mock_call, mock_semantic) -> None:
        # Revert case 3: passes length + fact gates (no numbers/names involved)
        # but changes what the speaker actually asserted.
        mock_call.return_value = '{"replacement": "サーベイはAIが自動で行います。"}'
        mock_semantic.return_value = (False, "confirmed_info contradicts replacement")
        result = reconstruct_span(
            span_target="最初やってみてもいいかもしれないです。",
            confirmed_info="サーベイは人間が行います。AIチャットインタビューは別の話。",
            api_key="test-key",
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "semantic_gate")

    @patch("reconstruct_span._call_llm_semantic_ok")
    @patch("reconstruct_span._call_llm_reconstruct")
    def test_success_path_all_guards_pass(self, mock_call, mock_semantic) -> None:
        mock_call.return_value = '{"replacement": "参加は任意の形式で良いとのことです。"}'
        mock_semantic.return_value = (True, "")
        result = reconstruct_span(
            span_target="記号を取ってる",
            context_before="前の文です。",
            context_after="後の文です。",
            confirmed_info="参加は任意の形式で良い。",
            api_key="test-key",
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.replacement, "参加は任意の形式で良いとのことです。")
        self.assertEqual(result.stage, "ok")

    @patch("reconstruct_span._call_llm_reconstruct")
    def test_skip_semantic_check_short_circuits(self, mock_call) -> None:
        mock_call.return_value = '{"replacement": "任意の形式で良いです。"}'
        result = reconstruct_span(
            span_target="記号を取ってる",
            confirmed_info="参加は任意の形式で良い。",
            api_key="test-key",
            skip_semantic_check=True,
        )
        self.assertTrue(result.ok)
        self.assertIn("semantic_skipped", result.reason)

    @patch("reconstruct_span._call_llm_reconstruct")
    def test_malformed_json_fails_closed(self, mock_call) -> None:
        mock_call.return_value = "not json at all"
        result = reconstruct_span(
            span_target="記号を取ってる",
            confirmed_info="参加は任意の形式で良い。",
            api_key="test-key",
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "llm_call")

    @patch("reconstruct_span._call_llm_reconstruct")
    def test_code_fenced_json_is_parsed(self, mock_call) -> None:
        mock_call.return_value = '```json\n{"replacement": "任意の形式で良いです。"}\n```'
        with patch("reconstruct_span._call_llm_semantic_ok", return_value=(True, "")):
            result = reconstruct_span(
                span_target="記号を取ってる",
                confirmed_info="参加は任意の形式で良い。",
                api_key="test-key",
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.replacement, "任意の形式で良いです。")


class ApplySpanReconstructionTests(unittest.TestCase):
    def test_apply_splices_only_the_span(self) -> None:
        text = "AAA[TARGET]BBB"
        start, end = text.index("[TARGET]"), text.index("[TARGET]") + len("[TARGET]")
        result = SpanReconstructResult(ok=True, replacement="XYZ", stage="ok")
        out = apply_span_reconstruction(text, start, end, result)
        self.assertEqual(out, "AAAXYZBBB")

    def test_apply_refuses_failed_result(self) -> None:
        result = SpanReconstructResult(ok=False, reason="fact_gate:numbers_missing", stage="fact_gate")
        with self.assertRaises(ValueError):
            apply_span_reconstruction("AAA[TARGET]BBB", 3, 11, result)


if __name__ == "__main__":
    unittest.main()
