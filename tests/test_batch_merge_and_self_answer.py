"""同一結論質問の統合・全出現置換・ナレッジ自己解決のテスト（2026-08-05）。"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import knowledge_self_answer as ksa
from recognition_batch import (
    VERIFY_TAG,
    _merge_items_with_same_candidate,
    apply_batch_corrections,
    build_batch_question_text,
)


class MergeSameCandidateTests(unittest.TestCase):
    def test_same_candidate_items_are_merged(self):
        items = [
            {"anomaly_id": "a1", "word": "山谷さん", "estimated_correction": "山屋さん"},
            {"anomaly_id": "a2", "word": "謝さん", "estimated_correction": "山屋さん"},
            {"anomaly_id": "a3", "word": "習字", "estimated_correction": "週次"},
        ]
        merged = _merge_items_with_same_candidate(items)
        self.assertEqual(len(merged), 2)
        primary = merged[0]
        self.assertEqual(primary["word"], "山谷さん")
        self.assertEqual(primary["merged_words"], ["謝さん"])
        self.assertEqual(primary["merged_anomaly_ids"], ["a2"])

    def test_no_candidate_and_span_hypothesis_not_merged(self):
        items = [
            {"anomaly_id": "a1", "word": "w1", "estimated_correction": ""},
            {"anomaly_id": "a2", "word": "w2", "estimated_correction": ""},
            {
                "anomaly_id": "a3",
                "word": "長い崩壊文",
                "estimated_correction": "仮説",
                "question_kind": "span_hypothesis",
            },
            {
                "anomaly_id": "a4",
                "word": "別の崩壊文",
                "estimated_correction": "仮説",
                "question_kind": "span_hypothesis",
            },
        ]
        merged = _merge_items_with_same_candidate(items)
        self.assertEqual(len(merged), 4)

    def test_question_text_shows_merged_words(self):
        items = [
            {
                "anomaly_id": "a1",
                "word": "山谷さん",
                "display": "山谷さんのイメージは",
                "estimated_correction": "山屋さん",
                "merged_words": ["謝さん"],
            }
        ]
        text = build_batch_question_text(items)
        self.assertIn("謝さん", text)
        self.assertIn("回答は全箇所に適用されます", text)


class ApplyAllOccurrencesTests(unittest.TestCase):
    def test_tagged_and_untagged_occurrences_all_replaced(self):
        # 従来は elif によりタグ付きが直るとタグなしが残った（山谷バグ）
        transcript = (
            f"山谷さん{VERIFY_TAG}の話。その後も山谷さんが説明。最後に山谷さんへ。"
        )
        parsed = [
            {"anomaly_id": "a1", "word": "山谷さん", "action": "correct", "correction": "山屋さん"}
        ]
        out, applied = apply_batch_corrections(transcript, parsed)
        self.assertNotIn("山谷", out)
        self.assertEqual(out.count("山屋さん"), 3)
        self.assertEqual(len(applied), 1)

    def test_merged_words_also_replaced(self):
        transcript = "山谷さんが来た。謝さんも来た。"
        parsed = [
            {
                "anomaly_id": "a1",
                "word": "山谷さん",
                "action": "correct",
                "correction": "山屋さん",
                "merged_words": ["謝さん"],
            }
        ]
        out, applied = apply_batch_corrections(transcript, parsed)
        self.assertNotIn("山谷", out)
        self.assertNotIn("謝さん", out)
        self.assertEqual(out.count("山屋さん"), 2)

    def test_correction_containing_word_not_double_applied(self):
        transcript = "シニア研修の件。シニアの件。"
        parsed = [
            {"anomaly_id": "a1", "word": "シニア", "action": "correct", "correction": "シニア研修"}
        ]
        out, _ = apply_batch_corrections(transcript, parsed)
        # correction が word を含むため2回目置換はスキップ＝元のまま
        self.assertEqual(out, transcript)

    def test_keep_removes_tags_for_merged_words(self):
        transcript = f"単語A{VERIFY_TAG}と単語B{VERIFY_TAG}。"
        parsed = [
            {
                "anomaly_id": "a1",
                "word": "単語A",
                "action": "keep",
                "correction": "",
                "merged_words": ["単語B"],
            }
        ]
        out, applied = apply_batch_corrections(transcript, parsed)
        self.assertNotIn(VERIFY_TAG, out)
        self.assertEqual(len(applied), 2)


class KnowledgeSelfAnswerTests(unittest.TestCase):
    def _setup_job(self, tmp):
        job_dir = os.path.join(tmp, "job_x")
        os.makedirs(job_dir)
        with open(
            os.path.join(job_dir, "meeting_profile.json"), "w", encoding="utf-8"
        ) as f:
            json.dump(
                {"relevant_knowledge": ["事前学習はUdemy Businessを活用する"]}, f
            )
        text_path = os.path.join(job_dir, "text.txt")
        with open(text_path, "w", encoding="utf-8") as f:
            f.write("教材は湯でみを使う。湯でみの講座がある。")
        unknowns_path = os.path.join(job_dir, "unknown_points.json")
        with open(unknowns_path, "w", encoding="utf-8") as f:
            json.dump(
                [
                    {
                        "anomaly_id": "u1",
                        "status": "open",
                        "anomaly_word": "湯でみ",
                        "reason": "教材名が確定できない",
                    }
                ],
                f,
                ensure_ascii=False,
            )
        return job_dir, text_path, unknowns_path

    def test_resolves_from_knowledge_and_replaces_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir, text_path, unknowns_path = self._setup_job(tmp)
            rows = [
                {"index": 1, "resolvable": True, "wrong": "湯でみ", "right": "Udemy",
                 "basis": "ナレッジにUdemy Businessと明記"}
            ]
            with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}):
                with mock.patch.object(ksa, "_ask_llm", return_value=rows):
                    n = ksa.resolve_unknowns_with_knowledge(
                        unknowns_path=unknowns_path,
                        text_path=text_path,
                        job_dir=job_dir,
                    )
            self.assertEqual(n, 1)
            text = open(text_path, encoding="utf-8").read()
            self.assertNotIn("湯でみ", text)
            self.assertEqual(text.count("Udemy"), 2)
            points = json.load(open(unknowns_path, encoding="utf-8"))
            self.assertEqual(points[0]["status"], "answered")
            self.assertEqual(
                points[0]["answered_by_question_id"], "knowledge_self_answer"
            )
            self.assertTrue(
                os.path.isfile(os.path.join(job_dir, ksa.AUDIT_FILENAME))
            )

    def test_answered_points_feed_knowledge_cascade(self):
        # 2026-08-07 回答カスケード: 同ジョブ内の回答済みQ&Aと確定修正が
        # 確定知識として供給されること（1回答で他の未解決を閉じる基盤）。
        with tempfile.TemporaryDirectory() as tmp:
            job_dir, _, _ = self._setup_job(tmp)
            points = [
                {
                    "status": "answered",
                    "anomaly_word": "山谷さん",
                    "answer": "山屋さんが正しい",
                },
                {"status": "open", "anomaly_word": "湯でみ"},
            ]
            with open(
                os.path.join(job_dir, "line_correction_audit.jsonl"),
                "w",
                encoding="utf-8",
            ) as f:
                f.write(
                    json.dumps(
                        {"wrong": "習字", "correct": "週次"},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            lines = ksa._load_knowledge_lines(job_dir, points)
        joined = "\n".join(lines)
        self.assertIn("山谷さん", joined)
        self.assertIn("山屋さんが正しい", joined)
        self.assertIn("習字", joined)
        self.assertIn("週次", joined)
        # open 項目は知識にしない
        self.assertNotIn("確認済み回答: 『湯でみ』", joined)

    def test_unresolvable_rows_do_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir, text_path, unknowns_path = self._setup_job(tmp)
            rows = [{"index": 1, "resolvable": False}]
            with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}):
                with mock.patch.object(ksa, "_ask_llm", return_value=rows):
                    n = ksa.resolve_unknowns_with_knowledge(
                        unknowns_path=unknowns_path,
                        text_path=text_path,
                        job_dir=job_dir,
                    )
            self.assertEqual(n, 0)
            self.assertIn(
                "湯でみ", open(text_path, encoding="utf-8").read()
            )

    def test_wrong_absent_from_text_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir, text_path, unknowns_path = self._setup_job(tmp)
            rows = [
                {"index": 1, "resolvable": True, "wrong": "存在しない語", "right": "Udemy"}
            ]
            with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}):
                with mock.patch.object(ksa, "_ask_llm", return_value=rows):
                    n = ksa.resolve_unknowns_with_knowledge(
                        unknowns_path=unknowns_path,
                        text_path=text_path,
                        job_dir=job_dir,
                    )
            self.assertEqual(n, 0)

    def test_llm_failure_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir, text_path, unknowns_path = self._setup_job(tmp)
            with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}):
                with mock.patch.object(
                    ksa, "_ask_llm", side_effect=RuntimeError("down")
                ):
                    n = ksa.resolve_unknowns_with_knowledge(
                        unknowns_path=unknowns_path,
                        text_path=text_path,
                        job_dir=job_dir,
                    )
            self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
