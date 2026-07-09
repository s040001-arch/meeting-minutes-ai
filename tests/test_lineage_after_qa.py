"""Regression: after_qa の系譜不変条件（2026-07-08 デイシス案件の汚染バグ）。

- Step 4.25 (contextual editor apply) は after_qa を書いてはならない
- Step 4.3 確定時、回答未反映の既存 after_qa は ai.txt から作り直す
- webhook の修正取り込みは scope 判定し、実在語を correction_dict に入れない
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch


class RefreshStaleAfterQaTests(unittest.TestCase):
    def _mk_job(self, tmp: str, *, ai: str, after_qa: str | None, answers: bool) -> str:
        job_dir = os.path.join(tmp, "job_x")
        os.makedirs(job_dir)
        with open(os.path.join(job_dir, "merged_transcript_ai.txt"), "w", encoding="utf-8") as f:
            f.write(ai)
        if after_qa is not None:
            with open(
                os.path.join(job_dir, "merged_transcript_after_qa.txt"), "w", encoding="utf-8"
            ) as f:
                f.write(after_qa)
        if answers:
            with open(os.path.join(job_dir, "answers.json"), "w", encoding="utf-8") as f:
                json.dump([{"question_id": "q1", "answer": "はい"}], f)
        return job_dir

    def test_stale_after_qa_is_refreshed_from_ai(self) -> None:
        from run_job_once import refresh_stale_after_qa

        with tempfile.TemporaryDirectory() as tmp:
            job_dir = self._mk_job(
                tmp,
                ai="富士山が見える構成です",  # 4.3 が修復済み
                after_qa="藤井さんが見える構成です",  # 4.25 先取り作成の汚染系譜
                answers=False,
            )
            log_path = os.path.join(tmp, "log.txt")
            self.assertTrue(refresh_stale_after_qa(job_dir, log_path))
            with open(
                os.path.join(job_dir, "merged_transcript_after_qa.txt"), encoding="utf-8"
            ) as f:
                self.assertEqual(f.read(), "富士山が見える構成です")

    def test_after_qa_with_answers_is_preserved(self) -> None:
        from run_job_once import refresh_stale_after_qa

        with tempfile.TemporaryDirectory() as tmp:
            job_dir = self._mk_job(
                tmp,
                ai="AI版のテキスト",
                after_qa="人の回答反映済みテキスト",
                answers=True,
            )
            log_path = os.path.join(tmp, "log.txt")
            self.assertFalse(refresh_stale_after_qa(job_dir, log_path))
            with open(
                os.path.join(job_dir, "merged_transcript_after_qa.txt"), encoding="utf-8"
            ) as f:
                self.assertEqual(f.read(), "人の回答反映済みテキスト")

    def test_missing_after_qa_is_noop(self) -> None:
        from run_job_once import refresh_stale_after_qa

        with tempfile.TemporaryDirectory() as tmp:
            job_dir = self._mk_job(tmp, ai="AI版", after_qa=None, answers=False)
            log_path = os.path.join(tmp, "log.txt")
            self.assertFalse(refresh_stale_after_qa(job_dir, log_path))
            self.assertFalse(
                os.path.isfile(os.path.join(job_dir, "merged_transcript_after_qa.txt"))
            )


class EditorApplyDoesNotWriteAfterQaTests(unittest.TestCase):
    def test_apply_writes_ai_but_not_after_qa(self) -> None:
        from contextual_editor import _apply_proposals_to_job

        with tempfile.TemporaryDirectory() as tmp:
            job_dir = os.path.join(tmp, "job_x")
            os.makedirs(job_dir)
            text = "こんにちは。これはテストです。"
            with patch.dict(os.environ, {"SEMANTIC_INTEGRITY_GATE_ENABLED": "0"}):
                _apply_proposals_to_job(job_dir, text, [], meeting_profile=None)
            self.assertTrue(
                os.path.isfile(os.path.join(job_dir, "merged_transcript_ai.txt"))
            )
            self.assertFalse(
                os.path.isfile(os.path.join(job_dir, "merged_transcript_after_qa.txt")),
                "Step 4.25 apply が after_qa を先取り作成すると "
                "Step 4.3 の修復が捨てられる（系譜バグ）",
            )


class WebhookCorrectionScopeGateTests(unittest.TestCase):
    def test_real_word_wrong_goes_to_learned_context_not_dict(self) -> None:
        import learned_corrections_store
        import webhook_app

        calls: list[dict] = []

        def fake_add(**kwargs):
            calls.append(kwargs)
            return {"action": "added", "reason": "ok"}

        with tempfile.TemporaryDirectory() as tmp:
            dict_path = os.path.join(tmp, "correction_dict.json")
            with (
                patch.object(webhook_app, "CORRECTION_DICT_PATH", dict_path),
                patch.object(learned_corrections_store, "add_learned_correction", fake_add),
            ):
                added, updated, learned = webhook_app._merge_correction_pairs(
                    [
                        # 実在語（3文字以下も含む）→ 盲目置換辞書には入れない
                        {"wrong": "富士山", "correct": "藤井さん"},
                        # 実在しにくい崩れ表記 → global で dict へ
                        {"wrong": "カイギサイマツリ", "correct": "会議体まつり"},
                    ],
                    job_id="job_test",
                )

            self.assertEqual(added, 1)
            self.assertEqual(updated, 0)
            self.assertEqual(learned, 1)

            with open(dict_path, encoding="utf-8") as f:
                saved = json.load(f)
            self.assertNotIn("富士山", saved)
            self.assertIn("カイギサイマツリ", saved)

            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["wrong"], "富士山")
            self.assertEqual(calls[0]["right"], "藤井さん")
            self.assertEqual(calls[0]["scope"], "context")
            self.assertEqual(calls[0]["job_id"], "job_test")

    def test_suggest_scope_treats_short_real_words_as_context(self) -> None:
        from learned_corrections_store import suggest_scope

        self.assertEqual(suggest_scope("富士山"), "context")
        self.assertEqual(suggest_scope("本社"), "context")
        self.assertEqual(suggest_scope("決済"), "context")


if __name__ == "__main__":
    unittest.main()
