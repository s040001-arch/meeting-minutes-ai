"""P1/P2 回帰テスト: free-text 回答の本文混入防止と番号分解適用。

thr ジョブで実証されたバグ:
- 「1番は「西脇さん」で正しい。2番は「西脇さん」ではなく「相原」。」という
  複数番号回答が、訂正ではなく回答文そのものとして本文2箇所に混入した。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pinpoint_answer_apply import (  # noqa: E402
    apply_answers,
    looks_like_full_answer_text,
)
from recorrect_from_line_answer import (  # noqa: E402
    _extract_numbered_question_targets,
    _extract_question_header_hypothesis,
    _parse_numbered_freetext_answer,
    _replace_standalone_all,
    _try_handle_numbered_freetext_answer,
)

THR_ANSWER = "1番は「西脇さん」で正しい。2番は「西脇さん」ではなく「相原」。"
THR_QUESTION = (
    "以下の2箇所は、いずれも「西脇さん」の誤認識でしょうか？\n"
    "1. 「この辺りに【白きさん】とかどんな感じられます」\n"
    "2. 「ま、【ハングラーさん】がおっしゃっている」"
)


class FullAnswerGuardTests(unittest.TestCase):
    """回答文そのものが置換語として本文へ入る経路の遮断。"""

    def test_multi_numbered_answer_is_flagged(self) -> None:
        self.assertTrue(looks_like_full_answer_text(THR_ANSWER))

    def test_short_correction_word_is_not_flagged(self) -> None:
        for w in ("西脇さん", "相原", "決裁", "Box", "日程確認書"):
            self.assertFalse(looks_like_full_answer_text(w), w)

    def test_long_multi_sentence_is_flagged(self) -> None:
        self.assertTrue(
            looks_like_full_answer_text("これは「A」のことです。そして「B」も違います。")
        )

    def test_apply_answers_rejects_full_answer_replacement(self) -> None:
        """apply_answers 経由でも回答文が span に入らないこと。"""
        text = "この辺りに白きさんとかどんな感じられます。"
        record = {
            "answer_text": THR_ANSWER,
            "question_id": "q1",
            "anomaly_word": "白きさん",
            "span_before": "この辺りに白きさんとかどんな感じられます",
            "span_start": 0,
        }
        updated, applied = apply_answers(text, [record])
        self.assertEqual(updated, text)  # 本文は無変更
        self.assertNotIn("1番は", updated)
        errors = [a for a in applied if a.get("error")]
        self.assertTrue(errors)
        self.assertIn("full_answer_text", str(errors[0].get("error")))


class NumberedAnswerParseTests(unittest.TestCase):
    def test_parse_thr_answer(self) -> None:
        parsed = _parse_numbered_freetext_answer(THR_ANSWER)
        self.assertEqual(parsed.get(1), "西脇さん")
        self.assertEqual(parsed.get(2), "相原")

    def test_parse_keep_and_correct_variants(self) -> None:
        parsed = _parse_numbered_freetext_answer("1はOK。2は「工数」です。")
        self.assertEqual(parsed.get(1), "__KEEP_HYPOTHESIS__")
        self.assertEqual(parsed.get(2), "工数")

    def test_extract_question_targets(self) -> None:
        targets = _extract_numbered_question_targets(THR_QUESTION)
        self.assertEqual(targets, {1: "白きさん", 2: "ハングラーさん"})

    def test_extract_header_hypothesis(self) -> None:
        self.assertEqual(
            _extract_question_header_hypothesis(THR_QUESTION), "西脇さん"
        )


class ReplaceStandaloneTests(unittest.TestCase):
    def test_replace_after_particle(self) -> None:
        text = "この辺りに白きさんとかどんな感じられます。"
        out, count = _replace_standalone_all(text, "白きさん", "西脇さん")
        self.assertEqual(count, 1)
        self.assertIn("西脇さん", out)
        self.assertNotIn("白きさん", out)

    def test_unique_occurrence_fallback(self) -> None:
        text = "ま、ハングラーさんがおっしゃっている。"
        out, count = _replace_standalone_all(text, "ハングラーさん", "相原")
        self.assertEqual(count, 1)
        self.assertIn("相原がおっしゃっている", out)


class NumberedFreetextEndToEndTests(unittest.TestCase):
    """thr 再現: 番号分解→各位置に正しい訂正が適用され、回答文は混入しない。"""

    def _setup_job(self, tmp: str) -> tuple[str, str]:
        job_id = "job_test_thr"
        job_dir = os.path.join(tmp, job_id)
        os.makedirs(job_dir)
        body = (
            "この辺りに白きさんとかどんな感じられます。\n"
            "中略。\n"
            "ま、ハングラーさんがおっしゃっている、そういう話でした。\n"
        )
        with open(
            os.path.join(job_dir, "merged_transcript_after_qa.txt"),
            "w",
            encoding="utf-8",
        ) as f:
            f.write(body)
        unknowns = [
            {
                "anomaly_id": "a1",
                "anomaly_word": "白きさん",
                "status": "asked",
                "asked_by_question_id": "q_thr_1",
                "source": "coherence_review",
            },
            {
                "anomaly_id": "a2",
                "anomaly_word": "ハングラーさん",
                "status": "asked",
                "asked_by_question_id": "q_thr_1",
                "source": "coherence_review",
            },
        ]
        with open(
            os.path.join(job_dir, "unknown_points.json"), "w", encoding="utf-8"
        ) as f:
            json.dump(unknowns, f, ensure_ascii=False)
        return job_id, job_dir

    def test_thr_injection_regression(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job_id, job_dir = self._setup_job(tmp)
            out_path = os.path.join(job_dir, "merged_transcript_after_qa.txt")
            handled = _try_handle_numbered_freetext_answer(
                job_id=job_id,
                input_root=tmp,
                question_text=THR_QUESTION,
                answer_text=THR_ANSWER,
                question_id="q_thr_1",
                out_path=out_path,
                input_path=None,
            )
            self.assertTrue(handled)
            with open(out_path, encoding="utf-8") as f:
                body = f.read()
            # 回答文が本文に混入していないこと
            self.assertNotIn("1番は", body)
            self.assertNotIn("ではなく", body)
            # 各番号の訂正が正しい位置に適用されていること
            self.assertIn("この辺りに西脇さんとかどんな感じられます", body)
            self.assertIn("ま、相原がおっしゃっている", body)
            self.assertNotIn("白きさん", body)
            self.assertNotIn("ハングラーさん", body)
            # unknown が answered になっていること
            with open(
                os.path.join(job_dir, "unknown_points.json"), encoding="utf-8"
            ) as f:
                unknowns = json.load(f)
            for item in unknowns:
                self.assertEqual(item["status"], "answered")

    def test_unparseable_number_reopens_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job_id, job_dir = self._setup_job(tmp)
            out_path = os.path.join(job_dir, "merged_transcript_after_qa.txt")
            handled = _try_handle_numbered_freetext_answer(
                job_id=job_id,
                input_root=tmp,
                question_text=THR_QUESTION,
                answer_text="1番は「西脇さん」で正しい。",  # 2番は未回答
                question_id="q_thr_1",
                out_path=out_path,
                input_path=None,
            )
            self.assertTrue(handled)
            with open(out_path, encoding="utf-8") as f:
                body = f.read()
            self.assertIn("西脇さん", body)
            self.assertIn("ハングラーさん", body)  # 未回答分は無変更
            with open(
                os.path.join(job_dir, "unknown_points.json"), encoding="utf-8"
            ) as f:
                unknowns = json.load(f)
            by_word = {u["anomaly_word"]: u for u in unknowns}
            self.assertEqual(by_word["白きさん"]["status"], "answered")
            self.assertEqual(by_word["ハングラーさん"]["status"], "open")

    def test_not_numbered_question_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job_id, _job_dir = self._setup_job(tmp)
            handled = _try_handle_numbered_freetext_answer(
                job_id=job_id,
                input_root=tmp,
                question_text="『白きさん』は誤認識でしょうか？",
                answer_text="西脇さんです",
                question_id="q_thr_2",
                out_path=os.path.join(tmp, job_id, "merged_transcript_after_qa.txt"),
                input_path=None,
            )
            self.assertFalse(handled)


class CorrectionPairsBodyApplyTests(unittest.TestCase):
    """P2: 確定ペアが辞書追加と同時に本文へも即時適用されること。"""

    def test_pair_applied_to_after_qa(self) -> None:
        from webhook_app import _apply_correction_pairs_to_transcript

        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            try:
                os.chdir(tmp)
                job_id = "job_test_p2"
                job_dir = os.path.join("data", "transcriptions", job_id)
                os.makedirs(job_dir)
                with open(
                    os.path.join(job_dir, "merged_transcript_after_qa.txt"),
                    "w",
                    encoding="utf-8",
                ) as f:
                    f.write("資料は精神とボックスにほぐして共有します。")
                applied = _apply_correction_pairs_to_transcript(
                    job_id, [{"wrong": "精神とボックス", "correct": "Box"}]
                )
                self.assertEqual(applied, 1)
                with open(
                    os.path.join(job_dir, "merged_transcript_after_qa.txt"),
                    encoding="utf-8",
                ) as f:
                    body = f.read()
                self.assertIn("Box", body)
                self.assertNotIn("精神とボックス", body)
            finally:
                os.chdir(cwd)

    def test_pair_not_found_skips(self) -> None:
        from webhook_app import _apply_correction_pairs_to_transcript

        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            try:
                os.chdir(tmp)
                job_id = "job_test_p2b"
                job_dir = os.path.join("data", "transcriptions", job_id)
                os.makedirs(job_dir)
                original = "本文にその語はありません。"
                with open(
                    os.path.join(job_dir, "merged_transcript_after_qa.txt"),
                    "w",
                    encoding="utf-8",
                ) as f:
                    f.write(original)
                applied = _apply_correction_pairs_to_transcript(
                    job_id, [{"wrong": "存在しない語", "correct": "何か"}]
                )
                self.assertEqual(applied, 0)
                with open(
                    os.path.join(job_dir, "merged_transcript_after_qa.txt"),
                    encoding="utf-8",
                ) as f:
                    self.assertEqual(f.read(), original)
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
