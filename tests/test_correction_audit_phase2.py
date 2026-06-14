"""Tests for Phase 2 correction audit and conservative auto-delete."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from correction_audit import (
    AUTO_DELETE_INLINE_MARKER,
    MAX_AUTO_DELETE_SPAN_CHARS,
    append_audit_section_to_structured_md,
    apply_conservative_auto_deletes,
    classify_anomaly_routing,
    format_audit_section_md,
    is_auto_delete_candidate,
    load_audit_log,
    merge_audit_entries,
    write_audit_log,
)
from recognition_batch import parse_single_coherence_answer


class MaterialityGateTests(unittest.TestCase):
    def test_low_filler_is_auto_delete_candidate(self) -> None:
        an = {
            "confidence": "low",
            "materiality": "low",
            "content_nature": "colloquial",
            "anomaly_word": "切れる",
        }
        self.assertTrue(is_auto_delete_candidate(an))

    def test_substantive_never_auto_delete(self) -> None:
        an = {
            "confidence": "low",
            "materiality": "high",
            "content_nature": "substantive",
            "anomaly_word": "85万円",
        }
        self.assertFalse(is_auto_delete_candidate(an))

    def test_unknown_nature_never_auto_delete(self) -> None:
        an = {
            "confidence": "low",
            "materiality": "low",
            "content_nature": "unknown",
            "anomaly_word": "切れる",
        }
        self.assertFalse(is_auto_delete_candidate(an))

    def test_medium_not_auto_delete(self) -> None:
        an = {
            "confidence": "medium",
            "materiality": "low",
            "content_nature": "filler",
            "anomaly_word": "切れる",
        }
        self.assertFalse(is_auto_delete_candidate(an))


class AutoDeleteModeTests(unittest.TestCase):
    def _sample(self) -> dict:
        return {
            "anomaly_id": "ta_x",
            "confidence": "low",
            "materiality": "low",
            "content_nature": "colloquial",
            "anomaly_word": "切れる",
            "span_start": 10,
            "span_end": 13,
            "span_text": "これで切れる",
            "reason": "比喩的口語",
        }

    def test_shadow_does_not_modify_text(self) -> None:
        text = "前の文。これで切れる。後の文。"
        out, entries = apply_conservative_auto_deletes(text, [self._sample()], mode="shadow")
        self.assertEqual(out, text)
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0]["applied"])

    def test_active_inserts_marker(self) -> None:
        text = "前の文。これで切れる。後の文。"
        out, entries = apply_conservative_auto_deletes(text, [self._sample()], mode="active")
        self.assertIn(AUTO_DELETE_INLINE_MARKER, out)
        self.assertNotIn("切れる", out)
        self.assertTrue(entries[0]["applied"])
        self.assertTrue(entries[0]["inline_marker"])

    def test_substantive_preserved(self) -> None:
        text = "価格は85万円です。"
        an = {
            "confidence": "low",
            "materiality": "high",
            "content_nature": "substantive",
            "anomaly_word": "85万円",
            "span_start": 3,
            "span_end": 7,
        }
        out, entries = apply_conservative_auto_deletes(text, [an], mode="active")
        self.assertEqual(out, text)
        self.assertEqual(entries, [])

    def test_long_span_not_auto_deleted(self) -> None:
        long_filler = "あ" * (MAX_AUTO_DELETE_SPAN_CHARS + 1)
        text = f"短い前文。{long_filler}。後文。"
        an = {
            "anomaly_id": "ta_long",
            "confidence": "low",
            "materiality": "low",
            "content_nature": "filler",
            "anomaly_word": long_filler,
            "reason": "test",
        }
        out, entries = apply_conservative_auto_deletes(text, [an], mode="active")
        self.assertEqual(out, text)
        self.assertEqual(entries, [])


class AuditLogMdTests(unittest.TestCase):
    def test_format_excludes_keep(self) -> None:
        md = format_audit_section_md(
            [
                {"action": "correct", "anomaly_id": "ta_1", "before": "A", "after": "B", "reason": "r"},
                {"action": "keep", "before": "X", "after": "X"},
            ]
        )
        self.assertIn("ta_1", md)
        self.assertNotIn("###", md[md.find("ta_1") + 10 :])  # only one entry block
        self.assertEqual(md.count("###"), 1)

    def test_delete_includes_restore_text(self) -> None:
        md = format_audit_section_md(
            [
                {
                    "action": "delete",
                    "anomaly_id": "ta_2",
                    "before": "これで切れる",
                    "restore_text": "これで切れる",
                    "span_start": 0,
                    "span_end": 6,
                    "reason": "口語",
                    "source": "coherence_auto_delete",
                    "applied": False,
                }
            ]
        )
        self.assertIn("復元用原文", md)
        self.assertIn("これで切れる", md)

    def test_append_to_structured_md(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            write_audit_log(
                os.path.join(tmp, "correction_audit_log.json"),
                [{"action": "correct", "anomaly_id": "ta_1", "before": "X", "after": "Y", "reason": "t"}],
            )
            base = "# Title\n\n## 管理情報\n\njob_id: j1\n"
            out = append_audit_section_to_structured_md(base, tmp)
            self.assertIn("## 補正・削除監査ログ", out)
            self.assertIn("X", out)
            self.assertIn("Y", out)


class RoutingClassificationTests(unittest.TestCase):
    def test_classify_buckets(self) -> None:
        self.assertEqual(
            classify_anomaly_routing({"auto_fixable": True}),
            "auto_fix",
        )
        self.assertEqual(
            classify_anomaly_routing(
                {
                    "confidence": "low",
                    "materiality": "low",
                    "content_nature": "filler",
                }
            ),
            "auto_delete_candidate",
        )
        self.assertEqual(
            classify_anomaly_routing({"confidence": "medium", "materiality": "high"}),
            "question",
        )


class ManualDeleteParseTests(unittest.TestCase):
    def test_delete_answer_parsed(self) -> None:
        parsed = parse_single_coherence_answer("削除", word="切れる")
        self.assertEqual(parsed["action"], "delete")


if __name__ == "__main__":
    unittest.main()
