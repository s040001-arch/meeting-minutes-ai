"""Tests for readable transcript pass."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from generate_minutes_transcript import build_minutes_text
from readable_transcript import (
    READABLE_TRANSCRIPT_FILENAME,
    _validate_chunk_output,
    generate_readable_transcript,
    is_readable_transcript_enabled,
    polish_transcript_text,
    readable_transcript_path,
    split_for_readable_edit,
)


class ReadableFlagTests(unittest.TestCase):
    def test_default_off(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(is_readable_transcript_enabled())

    def test_on_values(self) -> None:
        for val in ("1", "true", "yes", "on"):
            with patch.dict(os.environ, {"READABLE_TRANSCRIPT_ENABLED": val}, clear=False):
                self.assertTrue(is_readable_transcript_enabled())


class SplitTests(unittest.TestCase):
    def test_heading_preserved_as_segment(self) -> None:
        text = "### ▼価格の話\n\n本文A\n\n本文B"
        segments = split_for_readable_edit(text)
        kinds = [k for k, _ in segments]
        self.assertIn("heading", kinds)
        self.assertIn("body", kinds)
        heading = next(p for k, p in segments if k == "heading")
        self.assertTrue(heading.startswith("### ▼"))


class ValidationTests(unittest.TestCase):
    def test_rejects_missing_flagged_token(self) -> None:
        original = "6.5万円[要確認] について話した。"
        edited = "6.5万について話した。"
        self.assertFalse(_validate_chunk_output(original, edited))

    def test_accepts_flagged_token_preserved(self) -> None:
        original = "6.5万円[要確認] について話した。はい。ありがとうございました。"
        edited = "6.5万円[要確認] について話した。"
        self.assertTrue(_validate_chunk_output(original, edited))

    def test_accepts_sentence_prefixed_flag(self) -> None:
        original = "困りました。6.5万円[要確認] について。"
        edited = "6.5万円[要確認] について。"
        self.assertTrue(_validate_chunk_output(original, edited))


class PolishTests(unittest.TestCase):
    def test_no_api_key_returns_source(self) -> None:
        text = "はい。ありがとうございました。"
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(polish_transcript_text(text), text)

    def test_mock_edit_preserves_substance(self) -> None:
        source = (
            "75万円の話。85万円も出た。\n\n"
            "はい。ありがとうございました。お疲れ様でした。ござい。"
        )

        def fake_edit(_client, chunk, _system):
            return (
                chunk.replace("はい。", "")
                .replace("ありがとうございました。", "")
                .replace("ござい。", "")
            )

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=False):
            with patch("readable_transcript._edit_one_chunk", side_effect=fake_edit):
                out = polish_transcript_text(source)
        self.assertIn("75万円", out)
        self.assertIn("85万円", out)
        self.assertNotIn("ござい。", out)


class GenerateFileTests(unittest.TestCase):
    def test_does_not_modify_after_qa(self) -> None:
        after_qa = "75万円。6.5万円[要確認]。"
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = os.path.join(tmp, "job_test")
            os.makedirs(job_dir)
            after_path = os.path.join(job_dir, "merged_transcript_after_qa.txt")
            with open(after_path, "w", encoding="utf-8") as f:
                f.write(after_qa)

            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=False):
                with patch(
                    "readable_transcript._edit_one_chunk",
                    side_effect=lambda _c, chunk, _s: chunk,
                ):
                    generate_readable_transcript(job_dir=job_dir, source_text=after_qa)

            with open(after_path, encoding="utf-8") as f:
                self.assertEqual(f.read(), after_qa)
            readable = readable_transcript_path(job_dir)
            self.assertTrue(os.path.isfile(readable))
            self.assertEqual(os.path.basename(readable), READABLE_TRANSCRIPT_FILENAME)


class MinutesTextTests(unittest.TestCase):
    def test_section_label_switches(self) -> None:
        verbatim = build_minutes_text("t", "body")
        readable = build_minutes_text("t", "body", readable=True)
        self.assertIn("発言録（逐語）", verbatim)
        self.assertIn("発言録（整文）", readable)


if __name__ == "__main__":
    unittest.main()
