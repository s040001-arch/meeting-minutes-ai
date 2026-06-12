"""Tests for span_correction (Phase 1)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from span_correction import (
    apply_span_corrections_batch,
    apply_span_word_replacement,
    build_span_fields,
    resolve_word_position,
)


class SpanCorrectionTests(unittest.TestCase):
    def test_does_not_replace_kanji_inside_compound(self) -> None:
        text = "この意味は重要です。"
        pos = resolve_word_position(text, "味")
        self.assertEqual(pos, -1)
        new_text, changed = apply_span_word_replacement(
            text, start=1, wrong="味", right="X"
        )
        self.assertFalse(changed)
        self.assertEqual(new_text, text)

    def test_replaces_standalone_word_only(self) -> None:
        text = "意味は大きい。味がする。"
        pos = resolve_word_position(text, "味", hint_pos=text.find("味が"))
        self.assertGreaterEqual(pos, 0)
        self.assertEqual(text[pos : pos + 1], "味")
        new_text, changed = apply_span_word_replacement(
            text, start=pos, wrong="味", right="ミ"
        )
        self.assertTrue(changed)
        self.assertIn("意味", new_text)
        self.assertIn("ミがする", new_text)
        self.assertNotIn("意ミ", new_text)

    def test_batch_applies_from_end_to_start(self) -> None:
        text = "AAA BBB AAA"
        anomalies = [
            {
                "auto_fixable": True,
                "anomaly_word": "AAA",
                "span_corrected": "X",
                "span_start": 0,
                "anomaly_id": "ta_001",
                "confidence": "high",
                "reason": "t",
            },
            {
                "auto_fixable": True,
                "anomaly_word": "AAA",
                "span_corrected": "Y",
                "span_start": 8,
                "anomaly_id": "ta_002",
                "confidence": "high",
                "reason": "t",
            },
        ]
        out, applied = apply_span_corrections_batch(text, anomalies)
        self.assertEqual(out, "X BBB Y")
        self.assertEqual(len(applied), 2)

    def test_build_span_fields(self) -> None:
        text = "会議で味ンダーを確認した。"
        fields = build_span_fields(
            text,
            word="味ンダー",
            estimated="アジェンダ",
            context="味ンダー",
        )
        self.assertGreaterEqual(fields["span_start"], 0)
        self.assertIn("味ンダー", fields["span_text"])
        self.assertEqual(fields["span_corrected"], "アジェンダ")


class CoherenceAutoFixTests(unittest.TestCase):
    def test_apply_high_auto_fixes_span_local(self) -> None:
        from coherence_review import _apply_high_auto_fixes, _enrich_anomaly

        text = "意味は大きい。味ンダーを使う。会議のアジェンダを配布。味がする。"
        raw = {
            "context": "味ンダーを使う",
            "anomaly_word": "味ンダー",
            "estimated_correction": "アジェンダ",
            "confidence": "high",
            "reason": "test",
        }
        enriched = _enrich_anomaly(raw, 1, text)
        self.assertTrue(enriched.get("auto_fixable"))

        with tempfile.TemporaryDirectory() as tmp:
            auto_path = os.path.join(tmp, "auto_corrections.json")
            audit_path = os.path.join(tmp, "correction_audit_log.json")
            fixed, entries = _apply_high_auto_fixes(
                text, [enriched], auto_path, audit_path
            )
            self.assertEqual(len(entries), 1)
            self.assertIn("意味", fixed)
            self.assertIn("アジェンダ", fixed)
            self.assertIn("味がする", fixed)
            self.assertNotIn("味ンダー", fixed)

            with open(auto_path, encoding="utf-8") as f:
                auto_data = json.load(f)
            self.assertEqual(auto_data[0]["occurrences_replaced"], 1)
            self.assertIn("span_start", auto_data[0])

            with open(audit_path, encoding="utf-8") as f:
                audit_data = json.load(f)
            self.assertEqual(len(audit_data), 1)
            self.assertEqual(audit_data[0]["action"], "correct")
            self.assertEqual(audit_data[0]["confidence"], "high")


if __name__ == "__main__":
    unittest.main()
