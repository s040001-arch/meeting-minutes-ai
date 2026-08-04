"""Tests for readable transcript pass."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

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
    def test_rejects_extra_flagged_token(self) -> None:
        original = "工場がもう桐生をかとか、あと八戸になるので。"
        edited = "工場がもう[要確認]義理をかとか、あと八戸になるので。"
        self.assertFalse(_validate_chunk_output(original, edited))

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

        def fake_edit(_client, chunk, _system, **_kw):
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

    def test_sonnet5_request_omits_deprecated_temperature(self) -> None:
        from readable_transcript import _edit_one_chunk

        client = MagicMock()
        block = MagicMock(type="text", text="本文です。")
        client.messages.create.return_value = MagicMock(content=[block])
        self.assertEqual(
            _edit_one_chunk(client, "本文です。", "system", temperature=0.4),
            "本文です。",
        )
        self.assertNotIn(
            "temperature",
            client.messages.create.call_args.kwargs,
        )


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
                    side_effect=lambda _c, chunk, _s, **_kw: chunk,
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

    def test_notice_prepended_before_section(self) -> None:
        out = build_minutes_text(
            "t", "body", readable=True, notice="※一部区間は整文を適用できませんでした"
        )
        self.assertIn("※一部区間は整文を適用できませんでした", out)
        self.assertLess(
            out.index("※一部区間"), out.index("## 発言録（整文）")
        )

    def test_no_notice_by_default(self) -> None:
        out = build_minutes_text("t", "body", readable=True)
        self.assertNotIn("※一部区間", out)


class ChunkRetryTests(unittest.TestCase):
    """P4: 検証失敗チャンクの1回リトライと failed_chunk_idx 記録。"""

    def test_retry_succeeds_second_time(self) -> None:
        from readable_transcript import polish_transcript_text_with_stats

        source = "75万円の話。85万円も出た。"
        calls: list[int] = []

        def flaky_edit(_client, chunk, _system, **_kw):
            calls.append(1)
            if len(calls) == 1:
                return ""  # 1回目は検証失敗（空出力）
            return chunk

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=False):
            with patch("readable_transcript._edit_one_chunk", side_effect=flaky_edit):
                out, stats = polish_transcript_text_with_stats(source)
        self.assertEqual(len(calls), 2)
        self.assertEqual(stats["failed_chunk_idx"], [])
        self.assertEqual(stats["retried_ok"], 1)
        self.assertIn("75万円", out)

    def test_retry_fails_records_failed_chunk(self) -> None:
        from readable_transcript import polish_transcript_text_with_stats

        source = "75万円の話。85万円も出た。"

        def always_bad(_client, _chunk, _system, **_kw):
            return ""

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=False):
            with patch("readable_transcript._edit_one_chunk", side_effect=always_bad):
                out, stats = polish_transcript_text_with_stats(source)
        self.assertEqual(len(stats["failed_chunk_idx"]), 1)
        # 生テキスト採用（内容は保持される）
        self.assertIn("75万円", out)

    def test_long_failed_chunk_is_recovered_by_smaller_splits(self) -> None:
        from readable_transcript import polish_transcript_text_with_stats

        paragraph = "これは一般語の説明です。" * 70
        source = "\n\n".join([paragraph, paragraph, paragraph])

        def length_sensitive(_client, chunk, _system, **_kw):
            return "" if len(chunk) > 1800 else chunk

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=False):
            with patch(
                "readable_transcript._edit_one_chunk",
                side_effect=length_sensitive,
            ):
                out, stats = polish_transcript_text_with_stats(source)
        self.assertEqual(stats["failed_chunk_idx"], [])
        self.assertEqual(stats["split_recovered"], 1)
        self.assertIn("一般語の説明", out)


if __name__ == "__main__":
    unittest.main()
