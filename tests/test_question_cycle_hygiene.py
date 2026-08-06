"""質問サイクルの衛生機能（鮮度チェック・仮説生成）のテスト（2026-08-06）。"""
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch


class ResolveStaleUnknownsTests(unittest.TestCase):
    def test_stale_quotes_are_resolved(self) -> None:
        from run_question_cycle_once import _resolve_stale_unknowns

        with tempfile.TemporaryDirectory() as tmp:
            text_path = os.path.join(tmp, "text.txt")
            with open(text_path, "w", encoding="utf-8") as f:
                f.write("現在の本文にはこの断片だけが残っている。")
            unknowns_path = os.path.join(tmp, "unknown_points.json")
            points = [
                {
                    "status": "open",
                    "source": "final_review",
                    "text": "この断片だけが残っている",
                },
                {
                    "status": "open",
                    "source": "final_review",
                    "text": "既に修正されて消えた崩れ",
                },
                {
                    # 引用ベースでない検出は対象外
                    "status": "open",
                    "source": "claude_step9",
                    "text": "数値が矛盾している可能性",
                },
                {
                    "status": "answered",
                    "source": "final_review",
                    "text": "回答済みの消えた箇所",
                },
            ]
            with open(unknowns_path, "w", encoding="utf-8") as f:
                json.dump(points, f, ensure_ascii=False)

            resolved = _resolve_stale_unknowns(unknowns_path, text_path)
            with open(unknowns_path, encoding="utf-8") as f:
                after = json.load(f)

        self.assertEqual(resolved, 1)
        self.assertEqual(after[0]["status"], "open")
        self.assertEqual(after[1]["status"], "resolved")
        self.assertEqual(after[1]["resolved_via"], "stale_quote_gone")
        self.assertEqual(after[2]["status"], "open")
        self.assertEqual(after[3]["status"], "answered")


class KeepAsIsTests(unittest.TestCase):
    def test_trivially_equal_ignores_punctuation(self) -> None:
        from run_question_cycle_once import _trivially_equal

        self.assertTrue(_trivially_equal("話しててもなるほどね。", "話してても、なるほどね。"))
        self.assertTrue(_trivially_equal("同じ文", "同じ文"))
        self.assertFalse(_trivially_equal("どうなんか10月", "どうにか10月"))

    def test_keep_as_is_items_are_resolved_not_asked(self) -> None:
        from run_question_cycle_once import _drop_keep_as_is_items

        with tempfile.TemporaryDirectory() as tmp:
            unknowns_path = os.path.join(tmp, "unknown_points.json")
            points = [
                {
                    "status": "open",
                    "source": "final_review",
                    "anomaly_id": "final_same",
                    "text": "話しててもなるほどね。",
                },
                {
                    "status": "open",
                    "source": "final_review",
                    "anomaly_id": "final_diff",
                    "text": "どうなんか10月に来る",
                },
            ]
            with open(unknowns_path, "w", encoding="utf-8") as f:
                json.dump(points, f, ensure_ascii=False)

            items = [
                {
                    "anomaly_id": "final_same",
                    "text": "話しててもなるほどね。",
                    "estimated_correction": "話してても、なるほどね。",
                },
                {
                    "anomaly_id": "final_diff",
                    "text": "どうなんか10月に来る",
                    "estimated_correction": "どうにか10月に来る",
                },
            ]
            kept = _drop_keep_as_is_items(items, unknowns_path)
            with open(unknowns_path, encoding="utf-8") as f:
                after = json.load(f)

        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["anomaly_id"], "final_diff")
        by_id = {p["anomaly_id"]: p for p in after}
        self.assertEqual(by_id["final_same"]["status"], "resolved")
        self.assertEqual(
            by_id["final_same"]["resolved_via"], "hypothesis_equals_original_keep"
        )
        self.assertEqual(by_id["final_diff"]["status"], "open")


class GenerateHypothesesTests(unittest.TestCase):
    def _fake_response(self, payload: str):
        block = MagicMock()
        block.text = payload
        resp = MagicMock()
        resp.content = [block]
        return resp

    def test_hypotheses_added_for_items_without_fix(self) -> None:
        from run_question_cycle_once import _generate_hypotheses_for_items

        full_text = "前文。あの、すっぴんと新人って感じだよね。後文。"
        items = [
            {
                "text": "すっぴんと新人",
                "issue": "意味不明（誤認識の疑い）",
                "estimated_correction": "",
            },
            {
                "text": "既に候補あり",
                "estimated_correction": "既存の候補",
            },
        ]
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}):
            with patch("anthropic.Anthropic") as mock_cls:
                mock_cls.return_value.messages.create.return_value = (
                    self._fake_response(
                        '[{"index": 1, "candidate": "すっかり社会人"}]'
                    )
                )
                added = _generate_hypotheses_for_items(items, full_text)

        self.assertEqual(added, 1)
        self.assertEqual(items[0]["estimated_correction"], "すっかり社会人")
        self.assertTrue(items[0]["hypothesis_generated"])
        self.assertEqual(items[1]["estimated_correction"], "既存の候補")

    def test_llm_failure_is_fail_open(self) -> None:
        from run_question_cycle_once import _generate_hypotheses_for_items

        items = [{"text": "崩れ箇所", "estimated_correction": ""}]
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}):
            with patch("anthropic.Anthropic") as mock_cls:
                mock_cls.return_value.messages.create.side_effect = (
                    RuntimeError("api down")
                )
                added = _generate_hypotheses_for_items(items, "崩れ箇所を含む本文")
        self.assertEqual(added, 0)
        self.assertEqual(items[0]["estimated_correction"], "")


if __name__ == "__main__":
    unittest.main()
