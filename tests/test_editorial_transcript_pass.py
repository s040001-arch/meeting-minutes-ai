from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from editorial_transcript_pass import editorialize_transcript


class EditorialTranscriptPassTests(unittest.TestCase):
    def test_applies_reader_facing_rewrite_when_protected_facts_survive(self) -> None:
        source = (
            "土井様が88kgの話をしました。"
            "はい、あの、意味のない崩れです。重要な説明です。"
            "この説明には背景と理由があります。次の施策も説明します。"
        )

        def fake_create(**kwargs):
            user_text = kwargs["messages"][0]["content"]
            locked = user_text.split("\n\n", 1)[1]
            edited = locked.replace(
                "はい、あの、意味のない崩れです。", ""
            )
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=edited)]
            )

        client = MagicMock()
        client.messages.create.side_effect = fake_create
        with patch.dict(
            os.environ,
            {
                "EDITORIAL_TRANSCRIPT_ENABLED": "1",
                "ANTHROPIC_API_KEY": "test-key",
            },
            clear=False,
        ):
            with patch(
                "editorial_transcript_pass.anthropic.Anthropic",
                return_value=client,
            ):
                result, stats = editorialize_transcript(
                    source,
                    {"participants": ["土井様"]},
                )
        self.assertTrue(stats["applied"])
        self.assertFalse(stats["failed"])
        self.assertIn("土井様", result)
        self.assertIn("88kg", result)
        self.assertNotIn("意味のない崩れ", result)

    def test_fails_closed_when_model_drops_locked_fact(self) -> None:
        source = "土井様が88kgの話をしました。"
        client = MagicMock()
        client.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="話をしました。")]
        )
        with patch.dict(
            os.environ,
            {
                "EDITORIAL_TRANSCRIPT_ENABLED": "1",
                "ANTHROPIC_API_KEY": "test-key",
            },
            clear=False,
        ):
            with patch(
                "editorial_transcript_pass.anthropic.Anthropic",
                return_value=client,
            ):
                result, stats = editorialize_transcript(
                    source,
                    {"participants": ["土井様"]},
                )
        self.assertEqual(result, source)
        self.assertTrue(stats["failed"])
        self.assertTrue(
            any(
                "numeric_tokens_changed" in error
                or "honorific_names_changed" in error
                for error in stats["validation_errors"]
            )
        )

    def test_restores_changed_name_in_corresponding_paragraph(self) -> None:
        source = (
            "田中様がAI活用について説明しました。"
            "この後も重要な背景と理由を詳しく説明しました。"
        )
        client = MagicMock()
        client.messages.create.return_value = SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="text",
                    text=source.replace("田中様", "土井様").replace(
                        "この後も重要な背景と理由を詳しく説明しました。",
                        "続いて、背景と理由を詳しく説明しました。",
                    ),
                )
            ]
        )
        with patch.dict(
            os.environ,
            {
                "EDITORIAL_TRANSCRIPT_ENABLED": "1",
                "ANTHROPIC_API_KEY": "test-key",
            },
            clear=False,
        ):
            with patch(
                "editorial_transcript_pass.anthropic.Anthropic",
                return_value=client,
            ):
                result, stats = editorialize_transcript(source)
        self.assertIn("田中様", result)
        self.assertNotIn("土井様", result)
        self.assertTrue(stats["applied"])
        self.assertEqual(stats["fallback_chunk_idx"], [])
        self.assertEqual(stats["restored_token_paragraphs"], [0])


if __name__ == "__main__":
    unittest.main()
