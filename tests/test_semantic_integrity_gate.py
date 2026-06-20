"""Tests for semantic_integrity_gate unverified handling (Phase 2-b)."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from semantic_integrity_gate import (
    SemanticCheckResult,
    _llm_semantic_checks,
    _unverified_semantic_check,
    count_semantic_issue_types,
)


class SemanticUnverifiedTests(unittest.TestCase):
    def test_unverified_check_is_fail_closed(self) -> None:
        r = _unverified_semantic_check("pid-1", "missing_from_llm")
        self.assertFalse(r.ok)
        self.assertEqual(r.issue, "missing_from_llm")

    def test_count_semantic_issue_types(self) -> None:
        checks = [
            {"ok": False, "issue": "missing_from_llm"},
            {"ok": False, "issue": "no_llm_response"},
            {"ok": False, "issue": "meaning_loss"},
        ]
        counts = count_semantic_issue_types(checks)
        self.assertEqual(counts["missing_from_llm"], 1)
        self.assertEqual(counts["no_llm_response"], 1)

    @patch("semantic_integrity_gate._llm_semantic_checks_once")
    def test_missing_from_llm_fail_closed_without_retry(self, once) -> None:
        once.return_value = [
            SemanticCheckResult(ok=True, proposal_id="a", issue="none"),
            _unverified_semantic_check("b", "missing_from_llm"),
        ]
        with patch("semantic_integrity_gate.is_semantic_gate_retry_missing_enabled", return_value=False):
            results = _llm_semantic_checks(
                [{"proposal_id": "a"}, {"proposal_id": "b"}],
                api_key="test-key",
            )
        self.assertEqual(len(results), 2)
        missing = [r for r in results if r.proposal_id == "b"][0]
        self.assertFalse(missing.ok)
        self.assertEqual(missing.issue, "missing_from_llm")
        once.assert_called_once()

    @patch("semantic_integrity_gate._llm_semantic_checks_once")
    def test_retry_missing_runs_second_pass_when_enabled(self, once) -> None:
        once.side_effect = [
            [_unverified_semantic_check("b", "missing_from_llm")],
            [SemanticCheckResult(ok=True, proposal_id="b", issue="none")],
        ]
        with patch("semantic_integrity_gate.is_semantic_gate_retry_missing_enabled", return_value=True):
            results = _llm_semantic_checks([{"proposal_id": "b"}], api_key="test-key")
        self.assertEqual(once.call_count, 2)
        self.assertTrue(results[0].ok)


if __name__ == "__main__":
    unittest.main()
