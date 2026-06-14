"""Tests for Phase 4 incremental LINE answer reflection."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from coherence_review import _apply_review_tags
from line_answer_reflect import (
    VERIFY_TAG,
    apply_incremental_coherence_answer,
    ensure_after_qa_initialized,
    format_reflect_log,
    load_after_qa_text,
    save_after_qa_text,
)
from recognition_batch import parse_single_coherence_answer
from recorrect_from_line_answer import (
    _handle_coherence_single_answer,
    _is_no_answers_error,
    _resolve_answers_json_path,
    load_answer_record,
)
from span_correction import apply_span_word_replacement, resolve_word_position


def _sample_anomalies_for_tags() -> list[dict]:
    return [
        {
            "anomaly_word": "朝にされる",
            "confidence": "medium",
            "auto_fixable": False,
            "context_position_in_transcript": 5,
        },
        {
            "anomaly_word": "切れる",
            "confidence": "low",
            "auto_fixable": False,
            "context_position_in_transcript": 50,
        },
        {
            "anomaly_word": "85万円",
            "confidence": "low",
            "auto_fixable": False,
            "context_position_in_transcript": 80,
        },
    ]


def _synthetic_003337_transcript() -> str:
    return (
        "まず定価が7.5万円[要確認]なんですね。まず、前提として定価が7.5万円ですと。\n\n"
        "6.5万円[要確認]という定価の時代がありまして。概ね10%の割引価格を適用すると58万円。\n\n"
        "例えば75万とかに値上げした場合でも、熱費ができない[要確認]ってこと？宮本さん召喚[要確認]できないと。\n\n"
        "27万。1.5倍ぐらいの金額、10倍ぐらい[要確認]ですね。\n\n"
        "3年目研修と小さいやつ[要確認]は2つずつかな。\n\n"
        "2クラスになるのが3年目と小さいAさん[要確認]の、そうですね。\n\n"
        "切れる[要確認]は口語。85万円[要確認]は低材料性。\n"
    )


def _answers_sequence() -> list[dict]:
    return [
        {
            "question_id": "q1",
            "question_text": "7.5万円",
            "answer_text": "75万円",
            "unknown": {
                "anomaly_id": "ta_001",
                "anomaly_word": "7.5万円",
                "context_position_in_transcript": 5,
                "source": "coherence_review",
                "type": "coherence_review",
            },
        },
        {
            "question_id": "q2",
            "question_text": "6.5万円",
            "answer_text": "65万円",
            "unknown": {
                "anomaly_id": "ta_002",
                "anomaly_word": "6.5万円",
                "context_position_in_transcript": 60,
                "source": "coherence_review",
                "type": "coherence_review",
            },
        },
        {
            "question_id": "q3",
            "question_text": "熱費",
            "answer_text": "削除",
            "unknown": {
                "anomaly_id": "ta_004",
                "anomaly_word": "熱費ができない",
                "context_position_in_transcript": 120,
                "source": "coherence_review",
                "type": "coherence_review",
            },
        },
        {
            "question_id": "q4",
            "question_text": "10倍",
            "answer_text": "削除",
            "unknown": {
                "anomaly_id": "ta_003",
                "anomaly_word": "10倍ぐらい",
                "context_position_in_transcript": 200,
                "source": "coherence_review",
                "type": "coherence_review",
            },
        },
        {
            "question_id": "q5",
            "question_text": "小さいやつ",
            "answer_text": "正しい",
            "unknown": {
                "anomaly_id": "ta_009",
                "anomaly_word": "小さいやつ",
                "context_position_in_transcript": 240,
                "source": "coherence_review",
                "type": "coherence_review",
            },
        },
        {
            "question_id": "q6",
            "question_text": "小さいAさん",
            "answer_text": "正しい",
            "unknown": {
                "anomaly_id": "ta_010",
                "anomaly_word": "小さいAさん",
                "context_position_in_transcript": 290,
                "source": "coherence_review",
                "type": "coherence_review",
            },
        },
    ]


class LowTagSuppressionTests(unittest.TestCase):
    def test_low_items_not_tagged(self) -> None:
        text = "朝にされる。切れる。85万円。"
        out = _apply_review_tags(text, _sample_anomalies_for_tags())
        self.assertIn("朝にされる[要確認]", out)
        self.assertNotIn("切れる[要確認]", out)
        self.assertNotIn("85万円[要確認]", out)
        self.assertEqual(out.count("[要確認]"), 1)


class IncrementalReflectTests(unittest.TestCase):
    def test_synthetic_003337_six_answers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job_id = "job_test"
            job_dir = os.path.join(tmp, job_id)
            os.makedirs(job_dir)
            ai_path = os.path.join(job_dir, "merged_transcript_ai.txt")
            before_text = _synthetic_003337_transcript()
            with open(ai_path, "w", encoding="utf-8") as f:
                f.write(before_text)
            before_tags = before_text.count("[要確認]")

            for step in _answers_sequence():
                su = step["unknown"]
                parsed = parse_single_coherence_answer(step["answer_text"], word=su["anomaly_word"])
                qresult = {
                    "selected_unknown": su,
                    "question_text": step["question_text"],
                }
                with patch(
                    "recorrect_from_line_answer._persist_coherence_answer_to_learned_dict",
                    return_value={"action": "noop"},
                ):
                    _handle_coherence_single_answer(
                        job_id=job_id,
                        input_root=tmp,
                        question_result=qresult,
                        answer_text=step["answer_text"],
                        question_id=step["question_id"],
                        out_path=os.path.join(job_dir, "merged_transcript_after_qa.txt"),
                    )

            after = load_after_qa_text(job_dir)
            self.assertNotIn("7.5万円[要確認]", after)
            self.assertIn("75万円", after)
            self.assertNotIn("6.5万円[要確認]", after)
            self.assertIn("65万円", after)
            self.assertNotIn("10倍ぐらい[要確認]", after)
            self.assertNotIn("熱費ができない[要確認]", after)
            self.assertIn("小さいやつ", after)
            self.assertNotIn("小さいやつ[要確認]", after)
            self.assertIn("小さいAさん", after)
            self.assertNotIn("小さいAさん[要確認]", after)
            self.assertLess(after.count("[要確認]"), before_tags)

    def test_no_rollback_after_delete_then_keep(self) -> None:
        text = (
            "前文。10倍ぐらい[要確認]ですね。後文。\n\n"
            "小さいAさん[要確認]の続き。"
        )
        su_del = {
            "anomaly_id": "ta_003",
            "anomaly_word": "10倍ぐらい",
            "context_position_in_transcript": 3,
            "source": "coherence_review",
            "type": "coherence_review",
        }
        parsed_del = parse_single_coherence_answer("削除", word="10倍ぐらい")
        after_del, meta_del = apply_incremental_coherence_answer(
            text, unknown_item=su_del, parsed=parsed_del, question_id="q4"
        )
        self.assertTrue(meta_del.get("applied"))
        self.assertNotIn("10倍ぐらい[要確認]", after_del)

        su_keep = {
            "anomaly_id": "ta_010",
            "anomaly_word": "小さいAさん",
            "context_position_in_transcript": after_del.find("小さいAさん"),
            "source": "coherence_review",
            "type": "coherence_review",
        }
        parsed_keep = parse_single_coherence_answer("正しい", word="小さいAさん")
        after_keep, _ = apply_incremental_coherence_answer(
            after_del, unknown_item=su_keep, parsed=parsed_keep, question_id="q6"
        )
        self.assertNotIn("10倍ぐらい", after_keep)
        self.assertNotIn("小さいAさん[要確認]", after_keep)

    def test_observation_log_emitted(self) -> None:
        text = "定価が7.5万円[要確認]です。"
        su = {
            "anomaly_id": "ta_001",
            "anomaly_word": "7.5万円",
            "context_position_in_transcript": 3,
        }
        parsed = parse_single_coherence_answer("75万円", word="7.5万円")
        buf = io.StringIO()
        with redirect_stdout(buf):
            updated, meta = apply_incremental_coherence_answer(
                text, unknown_item=su, parsed=parsed, question_id="q1"
            )
            from line_answer_reflect import log_reflect_entry

            log_reflect_entry(meta)
        line = buf.getvalue()
        self.assertIn("line_answer_reflect_applied=", line)
        self.assertIn('"applied": true', line.replace("True", "true"))
        self.assertIn("75万円", updated)


class SpanSafetyTests(unittest.TestCase):
    def test_meaning_not_broken(self) -> None:
        text = "この意味は重要です。"
        pos = resolve_word_position(text, "味")
        self.assertEqual(pos, -1)
        new_text, changed = apply_span_word_replacement(
            text, start=1, wrong="味", right="X"
        )
        self.assertFalse(changed)
        self.assertEqual(new_text, text)

    def test_correct_does_not_touch_untagged_other_occurrence(self) -> None:
        text = "7.5万円です。定価が7.5万円[要確認]なんですね。"
        su = {
            "anomaly_word": "7.5万円",
            "context_position_in_transcript": text.find("7.5万円[要確認]"),
        }
        parsed = parse_single_coherence_answer("75万円", word="7.5万円")
        out, meta = apply_incremental_coherence_answer(
            text, unknown_item=su, parsed=parsed, question_id="q1"
        )
        self.assertTrue(meta.get("applied"))
        self.assertIn("7.5万円です。", out)
        self.assertIn("75万円", out)
        self.assertNotIn("7.5万円[要確認]", out)


class AnswersJsonTests(unittest.TestCase):
    def test_resolve_prefers_job_answers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job_id = "job_x"
            job_dir = os.path.join(tmp, job_id)
            os.makedirs(job_dir)
            job_answers = os.path.join(job_dir, "answers.json")
            with open(job_answers, "w", encoding="utf-8") as f:
                json.dump([{"answer_text": "x"}], f)
            path = _resolve_answers_json_path(job_id, tmp, None)
            self.assertEqual(path, job_answers)

    def test_empty_answers_is_no_answers_error(self) -> None:
        self.assertTrue(_is_no_answers_error(ValueError("answers json must be a non-empty JSON array.")))

    def test_load_answer_record_empty_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "answers.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump([], f)
            with self.assertRaises(ValueError):
                load_answer_record(path)


if __name__ == "__main__":
    unittest.main()
