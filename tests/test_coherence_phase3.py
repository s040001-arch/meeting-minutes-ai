"""Tests for Phase 3 coherence routing and question generation."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from coherence_review import _coherence_to_unknown_points, _enrich_anomaly
from recognition_batch import parse_single_coherence_answer
from run_question_cycle_once import (
    COHERENCE_SNIPPET_MAX,
    COHERENCE_SNIPPET_RADIUS,
    _build_coherence_question_text,
    _extract_snippet_around_word,
)


def _sample_anomalies() -> list[dict]:
    return [
        {
            "anomaly_id": "ta_001",
            "anomaly_word": "朝にされる",
            "confidence": "medium",
            "auto_fixable": False,
            "estimated_correction": "当てられる",
            "span_text": "…予定が空いてる人が朝にされるってことすか？…",
            "span_corrected": "…予定が空いてる人が当てられるってことすか？…",
            "context": "朝にされる",
            "reason": "人員配置文脈",
            "anomaly_type": "B",
            "context_position_in_transcript": 100,
        },
        {
            "anomaly_id": "ta_002",
            "anomaly_word": "切れる",
            "confidence": "low",
            "auto_fixable": False,
            "estimated_correction": "",
            "span_text": "…これで切れるっていうこと？…",
            "context": "切れる",
            "reason": "口語比喩",
            "anomaly_type": "E",
            "context_position_in_transcript": 200,
        },
        {
            "anomaly_id": "ta_003",
            "anomaly_word": "プレイマネジメント",
            "confidence": "high",
            "auto_fixable": True,
            "estimated_correction": "プレマネジメント",
            "span_text": "…プレイマネジメント、あ、プレマネジメント研修…",
            "span_corrected": "…プレマネジメント、あ、プレマネジメント研修…",
            "context": "プレイマネジメント",
            "reason": "自己訂正",
            "anomaly_type": "E",
            "context_position_in_transcript": 300,
        },
        {
            "anomaly_id": "ta_004",
            "anomaly_word": "85万円",
            "confidence": "low",
            "auto_fixable": False,
            "estimated_correction": "",
            "context": "85万円",
            "reason": "数値",
            "anomaly_type": "E",
            "context_position_in_transcript": 400,
        },
    ]


class CoherenceRoutingTests(unittest.TestCase):
    def test_low_excluded_from_question_queue(self) -> None:
        queued = _coherence_to_unknown_points(_sample_anomalies())
        words = {q["anomaly_word"] for q in queued}
        self.assertIn("朝にされる", words)
        self.assertNotIn("切れる", words)
        self.assertNotIn("85万円", words)

    def test_high_auto_fix_excluded(self) -> None:
        queued = _coherence_to_unknown_points(_sample_anomalies())
        words = {q["anomaly_word"] for q in queued}
        self.assertNotIn("プレイマネジメント", words)

    def test_enrich_preserves_span_fields(self) -> None:
        text = "予定が空いてる人が朝にされるってことすか？"
        raw = {
            "anomaly_word": "朝にされる",
            "estimated_correction": "当てられる",
            "span_text": "予定が空いてる人が朝にされるってこと",
            "span_corrected": "予定が空いてる人が当てられるってこと",
            "confidence": "medium",
            "reason": "test",
        }
        enriched = _enrich_anomaly(raw, 1, text)
        self.assertEqual(enriched["anomaly_word"], "朝にされる")
        self.assertEqual(enriched["estimated_correction"], "当てられる")
        self.assertIn("朝にされる", enriched["span_text"])
        self.assertIn("当てられる", enriched["span_corrected"])


class CoherenceQuestionTextTests(unittest.TestCase):
    def test_with_candidate_shows_suggestion_and_delete(self) -> None:
        item = {
            "anomaly_word": "朝にされる",
            "span_text": "…人が【朝にされる】って…",
            "span_corrected": "…人が当てられるって…",
            "estimated_correction": "当てられる",
        }
        text = _build_coherence_question_text(item)
        self.assertIn("当てられる", text)
        self.assertIn("「正しい」", text)
        self.assertIn("「削除」", text)
        self.assertNotIn("音声認識誤り", text)

    def test_without_candidate_open_form_and_delete(self) -> None:
        item = {
            "anomaly_word": "方の味だ",
            "span_text": "…開催のきっかけとなった方の味だという形…",
            "span_corrected": "",
            "estimated_correction": "",
        }
        text = _build_coherence_question_text(item)
        self.assertIn("この文脈に合いません", text)
        self.assertIn("「削除」", text)
        self.assertNotIn("では？", text)


class CoherenceAnswerParseTests(unittest.TestCase):
    def test_delete_answer(self) -> None:
        parsed = parse_single_coherence_answer("削除", word="切れる")
        self.assertEqual(parsed["action"], "delete")

    def test_keep_tadashii(self) -> None:
        parsed = parse_single_coherence_answer("正しい", word="朝にされる")
        self.assertEqual(parsed["action"], "keep")

    def test_correction_word(self) -> None:
        parsed = parse_single_coherence_answer("アサインされる", word="朝にされる")
        self.assertEqual(parsed["action"], "correct")
        self.assertEqual(parsed["correction"], "アサインされる")


class CoherenceSnippetExpansionTests(unittest.TestCase):
    def test_expanded_radius_and_sentence_context(self) -> None:
        self.assertGreaterEqual(COHERENCE_SNIPPET_RADIUS, 150)
        self.assertLessEqual(COHERENCE_SNIPPET_RADIUS, 200)
        self.assertGreaterEqual(COHERENCE_SNIPPET_MAX, 350)
        self.assertLessEqual(COHERENCE_SNIPPET_MAX, 400)

        lead = "。".join([f"前置きセンテンス{n}です" for n in range(12)]) + "。"
        full = (
            lead
            + "予算は85万円で合意しました。"
            + "次に人員配置の話です。"
            + "予定が空いてる人が朝にされるってことすか？"
            + "その後は別論点に移ります。"
            + lead
        )
        word = "朝にされる"
        pos = full.find(word)
        self.assertGreater(pos, 0)
        old_start = max(0, pos - 25)
        old_end = min(len(full), pos + len(word) + 25)
        old = full[old_start:old_end]
        new = _extract_snippet_around_word(full, word, pos)
        self.assertIn("85万円", new)
        self.assertNotIn("85万円", old)
        self.assertGreater(len(new), len(old))
        self.assertIn(word, new)
        self.assertLessEqual(len(new), COHERENCE_SNIPPET_MAX + 2)


if __name__ == "__main__":
    unittest.main()
