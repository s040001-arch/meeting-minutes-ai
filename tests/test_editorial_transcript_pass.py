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
                {
                    "index": item["index"],
                    "text": item["text"].replace(
                        "はい、あの、意味のない崩れです。", ""
                    ),
                }
                for item in paragraphs
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
                    text=json.dumps(
                        [{"index": 0, "text": "話をしました。"}],
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
                            {
                                "index": 0,
                                "text": source.replace(
                                    "田中様", "土井様"
                                ).replace(
                                    "この後も重要な背景と理由を詳しく説明しました。",
                                    "続いて、背景と理由を詳しく説明しました。",
                                ),
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
                                [
                                    {
                                        "index": 0,
                                        "text": "AI活用の背景と理由が説明されました。",
                                    }
                                ],
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

    def test_long_transcript_is_edited_in_position_independent_batches(
        self,
    ) -> None:
        # 後半の段落も前半と同じ小さなバッチで処理され、位置により
        # 品質が変わらないこと（1回の長い生成に依存しないこと）を固定する。
        paragraphs = [
            f"第{i}議題の説明です。えー、あの、補足します。" + "内容です。" * 10
            for i in range(6)
        ]
        source = "\n\n".join(paragraphs)

        def fake_create(**kwargs):
            payload = json.loads(
                kwargs["messages"][0]["content"].split("\n\n", 1)[1]
            )
            edited = [
                {
                    "index": item["index"],
                    "text": item["text"].replace("えー、あの、", ""),
                }
                for item in payload
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
                with patch(
                    "editorial_transcript_pass.EDITORIAL_BATCH_TARGET_CHARS",
                    120,
                ):
                    result, stats = editorialize_transcript(source)
        self.assertGreater(client.messages.create.call_count, 1)
        self.assertGreater(stats["total_batches"], 1)
        self.assertFalse(stats["failed"])
        self.assertEqual(stats["applied_paragraphs"], 6)
        self.assertNotIn("えー、あの、", result)
        self.assertIn("第5議題", result)

    def test_failed_batch_degrades_only_its_own_paragraphs(self) -> None:
        paragraphs = [
            "前半の議題です。えー、あの、補足します。" + "内容です。" * 10,
            "後半の議題です。えー、あの、補足します。" + "内容です。" * 10,
        ]
        source = "\n\n".join(paragraphs)

        def fake_create(**kwargs):
            payload = json.loads(
                kwargs["messages"][0]["content"].split("\n\n", 1)[1]
            )
            if payload[0]["index"] == 0:
                raise RuntimeError("boom")
            edited = [
                {
                    "index": item["index"],
                    "text": item["text"].replace("えー、あの、", ""),
                }
                for item in payload
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
                with patch(
                    "editorial_transcript_pass.EDITORIAL_BATCH_TARGET_CHARS",
                    120,
                ):
                    result, stats = editorialize_transcript(source)
        self.assertFalse(stats["failed"])
        self.assertEqual(stats["fallback_chunk_idx"], [0])
        self.assertIn("前半の議題です。えー、あの、", result)
        self.assertNotIn("後半の議題です。えー、あの、", result)

    def test_reordered_sentences_do_not_swap_protected_numbers(self) -> None:
        from editorial_transcript_pass import _NUMBER_RE, _restore_ordered_tokens

        source = "営業側が3人参加します。開発側が5人参加します。"
        reordered = "開発側が5人参加します。営業側が3人参加します。"
        restored, changed = _restore_ordered_tokens(
            source,
            reordered,
            _NUMBER_RE,
        )
        self.assertEqual(restored, reordered)
        self.assertFalse(changed)

    def test_clean_transcript_without_edits_is_not_a_failure(self) -> None:
        source = "議題の説明です。決定事項の確認です。"
        client = MagicMock()
        client.messages.create.return_value = SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="text",
                    text=json.dumps(
                        [{"index": 0, "text": source}],
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
        self.assertEqual(result, source)
        self.assertFalse(stats["failed"])
        self.assertFalse(stats["applied"])
        self.assertEqual(stats["validation_errors"], [])

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

    def test_low_confidence_reader_blocking_garble_is_resolved(self) -> None:
        text = "重要な話の途中で、あんなにも回ってるって何とありました。続きです。"
        finding = {
            "type": "unnatural",
            "quote": "あんなにも回ってるって何",
            "issue": "意味不明な崩れ断片",
            "fix": "",
            "confidence": "low",
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
                                "replacement": "案内業務が回っている理由",
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
        self.assertIn("案内業務が回っている理由", result)
        self.assertEqual(len(applied), 1)
        self.assertEqual(skipped, [])

    def test_unresolvable_garble_is_left_for_question_not_deleted(self) -> None:
        # モデルが確信を持てない場合は本文を変えず、質問経路に委ねる。
        text = "前文。見学者タイトルを回ってるような使い方。後文。"
        finding = {
            "type": "fragment",
            "quote": "見学者タイトルを回ってる",
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
                        [{"index": 0, "replacement": ""}],
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
        self.assertEqual(result, text)
        self.assertEqual(applied, [])
        self.assertEqual(len(skipped), 1)


if __name__ == "__main__":
    unittest.main()
