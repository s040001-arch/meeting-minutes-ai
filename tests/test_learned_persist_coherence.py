"""Tests for coherence learned-dict persistence skips."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from recorrect_from_line_answer import _persist_coherence_answer_to_learned_dict


class LearnedPersistCoherenceTests(unittest.TestCase):
    def _base_qr(self) -> dict:
        return {
            "selected_unknown": {
                "source": "coherence_review",
                "anomaly_word": "全面投手",
            }
        }

    @patch("learned_corrections_store.add_learned_correction")
    def test_delete_not_registered(self, mock_add) -> None:
        r = _persist_coherence_answer_to_learned_dict(
            job_id="job_test",
            input_root="data/transcriptions",
            question_result=self._base_qr(),
            answer_text="削除",
            base_text="全面投手[要確認]です。",
        )
        self.assertEqual(r.get("action"), "skipped")
        self.assertEqual(r.get("reason"), "action_delete")
        mock_add.assert_not_called()

    @patch("learned_corrections_store.add_learned_correction")
    def test_keep_not_registered(self, mock_add) -> None:
        r = _persist_coherence_answer_to_learned_dict(
            job_id="job_test",
            input_root="data/transcriptions",
            question_result=self._base_qr(),
            answer_text="正しい",
            base_text="全面投手[要確認]です。",
        )
        self.assertEqual(r.get("action"), "skipped")
        self.assertEqual(r.get("reason"), "action_keep")
        mock_add.assert_not_called()

    @patch("learned_corrections_store.add_learned_correction")
    def test_correct_registered(self, mock_add) -> None:
        mock_add.return_value = {"action": "added", "wrong": "全面投手", "right": "全面通り"}
        r = _persist_coherence_answer_to_learned_dict(
            job_id="job_test",
            input_root="data/transcriptions",
            question_result=self._base_qr(),
            answer_text="全面通り",
            base_text="全面投手[要確認]です。",
        )
        self.assertEqual(r.get("action"), "added")
        mock_add.assert_called_once()


if __name__ == "__main__":
    unittest.main()
