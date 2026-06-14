"""Tests for LINE completion_kind labels."""
from __future__ import annotations

import unittest

from line_send_question import build_line_message


class LineCompletionKindTests(unittest.TestCase):
    def test_full_completion_label(self) -> None:
        msg = build_line_message(
            {"question_status": "none", "completion_kind": "full", "message": "完了です。"}
        )
        self.assertTrue(msg.startswith("[完了]"))

    def test_coherence_done_label(self) -> None:
        msg = build_line_message(
            {
                "question_status": "none",
                "completion_kind": "coherence_done",
                "message": "音声認識ゆれの確認は完了しました。",
            }
        )
        self.assertTrue(msg.startswith("[認識ゆれの確認は完了]"))
        self.assertNotIn("[完了]", msg.splitlines()[0])


if __name__ == "__main__":
    unittest.main()
