from __future__ import annotations

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from editorial_transcript_pass import (
    editorialize_transcript,
    resolve_reader_blocking_findings,
)


class EditorialTranscriptPassTests(unittest.TestCase):
    def test_applies_reader_facing_rewrite_when_protected_facts_survive(self) -> None:
        source = (
            "土井様が88kgの話をしました。"
            "はい、あの、意味のない崩れです。重要な説明です。"
            "この説明には背景と理由があります。次の施策も説明します。"
        )

        def fake_create(**kwargs):
            user_text = kwargs["messages"][0]["content"]
            paragraphs = json.loads(user_text.split("\n\n", 1)[1])
            edited = [
                paragraph.replace(
                    "はい、あの、意味のない崩れです。", ""
                )
                for paragraph in paragraphs
            ]
            return SimpleNamespace(
                content=[
                    SimpleNamespace(
                        type="text",
                        text=json.dumps(edited, ensure_ascii=False),
                    )
                ]
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
            content=[
                SimpleNamespace(
                    type="text",
                    text=json.dumps(["話をしました。"], ensure_ascii=False),
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
                    text=json.dumps(
                        [
                            source.replace("田中様", "土井様").replace(
                                "この後も重要な背景と理由を詳しく説明しました。",
                                "続いて、背景と理由を詳しく説明しました。",
                            )
                        ],
                        ensure_ascii=False,
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

    def test_repairs_paragraph_when_protected_name_was_removed(self) -> None:
        source = (
            "田中様がAI活用について説明しました。"
            "この説明には重要な背景と理由があります。"
        )

        def fake_create(**kwargs):
            if "段落配列" in kwargs["messages"][0]["content"]:
                return SimpleNamespace(
                    content=[
                        SimpleNamespace(
                            type="text",
                            text=json.dumps(
                                ["AI活用の背景と理由が説明されました。"],
                                ensure_ascii=False,
                            ),
                        )
                    ]
                )
            return SimpleNamespace(
                content=[
                    SimpleNamespace(
                        type="text",
                        text="田中様から、AI活用の重要な背景と理由が説明されました。",
                    )
                ]
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
                result, stats = editorialize_transcript(source)
        self.assertIn("田中様", result)
        self.assertTrue(stats["applied"])
        self.assertEqual(stats["repaired_paragraphs"], [0])
        self.assertEqual(stats["fallback_chunk_idx"], [])

    def test_resolves_non_factual_reader_blocking_garble(self) -> None:
        text = "店舗指導のテープやここに読むと、現場データと統合できます。"
        finding = {
            "type": "fragment",
            "quote": "店舗指導のテープやここに読むと",
            "issue": "意味不明な崩れ断片",
            "fix": "",
            "confidence": "medium",
        }
        client = MagicMock()
        client.messages.create.return_value = SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="text",
                    text=json.dumps(
                        [
                            {
                                "index": 0,
                                "replacement": "店舗指導のノウハウを読み込ませると",
                            }
                        ],
                        ensure_ascii=False,
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
                result, applied, skipped = resolve_reader_blocking_findings(
                    text=text,
                    findings=[finding],
                )
        self.assertIn("店舗指導のノウハウ", result)
        self.assertEqual(len(applied), 1)
        self.assertEqual(skipped, [])


if __name__ == "__main__":
    unittest.main()
