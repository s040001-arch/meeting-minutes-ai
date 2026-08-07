from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from shadow_single_pass_editor import (
    _job_answer_context,
    _safe_profile,
    run_shadow,
)


class ShadowSinglePassEditorTests(unittest.TestCase):
    def test_job_answer_context_keeps_only_human_answers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            (job_dir / "unknown_points.json").write_text(
                json.dumps(
                    [
                        {
                            "status": "answered",
                            "span_text": "山谷さん",
                            "estimated_correction": "山屋さん",
                            "answer": "はい",
                            "auto_applied": False,
                        },
                        {
                            "status": "answered",
                            "span_text": "個数",
                            "answer": "工数",
                            "auto_applied": True,
                        },
                        {
                            "status": "open",
                            "span_text": "不明",
                            "answer": "",
                        },
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            answers = _job_answer_context(job_dir)
        self.assertEqual(
            answers,
            [
                {
                    "原文箇所": "山谷さん",
                    "提示候補": "山屋さん",
                    "ユーザー回答": "はい",
                }
            ],
        )

    def test_safe_profile_excludes_learned_knowledge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            (job_dir / "meeting_profile.json").write_text(
                json.dumps(
                    {
                        "title": "会議",
                        "participants": ["相原"],
                        "prior_context": "事前メール",
                        "relevant_knowledge": ["個数→工数"],
                        "learned_corrections": {"個数": "工数"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            profile = _safe_profile(job_dir)
        self.assertEqual(profile["title"], "会議")
        self.assertEqual(profile["prior_context"], "事前メール")
        self.assertNotIn("relevant_knowledge", profile)
        self.assertNotIn("learned_corrections", profile)

    def test_shadow_writes_only_shadow_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            source_path = job_dir / "merged_transcript.txt"
            source_path.write_text("えっと、価格は75万円です。", encoding="utf-8")
            (job_dir / "unknown_points.json").write_text(
                '[{"status":"pending"}]', encoding="utf-8"
            )
            before_unknowns = (job_dir / "unknown_points.json").read_bytes()

            client = MagicMock()
            client.messages.create.return_value = SimpleNamespace(
                content=[
                    SimpleNamespace(
                        type="text", text="価格は75万円です。"
                    )
                ],
                stop_reason="end_turn",
            )
            with patch.dict(
                os.environ, {"ANTHROPIC_API_KEY": "test"}, clear=False
            ), patch(
                "shadow_single_pass_editor.anthropic.Anthropic",
                return_value=client,
            ):
                output, report = run_shadow(
                    job_dir=job_dir, model="claude-opus-5"
                )

            self.assertEqual(output.read_text(encoding="utf-8"), "価格は75万円です。\n")
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertTrue(payload["shadow_only"])
            self.assertFalse(payload["published"])
            self.assertEqual(
                (job_dir / "unknown_points.json").read_bytes(),
                before_unknowns,
            )
            self.assertEqual(
                source_path.read_text(encoding="utf-8"),
                "えっと、価格は75万円です。",
            )
            self.assertFalse((job_dir / "minutes_structured.md").exists())

    def test_shadow_can_include_job_answers_without_mutating_them(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            (job_dir / "merged_transcript.txt").write_text(
                "山谷さんに確認します。", encoding="utf-8"
            )
            answers_path = job_dir / "unknown_points.json"
            answers_path.write_text(
                json.dumps(
                    [
                        {
                            "status": "answered",
                            "span_text": "山谷さん",
                            "estimated_correction": "山屋さん",
                            "answer": "はい",
                            "auto_applied": False,
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            before = answers_path.read_bytes()
            client = MagicMock()
            client.messages.create.return_value = SimpleNamespace(
                content=[
                    SimpleNamespace(
                        type="text", text="山屋さんに確認します。"
                    )
                ],
                stop_reason="end_turn",
            )
            with patch.dict(
                os.environ, {"ANTHROPIC_API_KEY": "test"}, clear=False
            ), patch(
                "shadow_single_pass_editor.anthropic.Anthropic",
                return_value=client,
            ):
                _, report = run_shadow(
                    job_dir=job_dir,
                    model="claude-opus-5",
                    include_job_answers=True,
                )
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(payload["job_answers_used"], 1)
            self.assertEqual(answers_path.read_bytes(), before)

    def test_incomplete_long_output_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            (job_dir / "merged_transcript.txt").write_text(
                "長い入力です。" * 100, encoding="utf-8"
            )
            client = MagicMock()
            client.messages.create.return_value = SimpleNamespace(
                content=[SimpleNamespace(type="text", text="途中まで")],
                stop_reason="max_tokens",
            )
            with patch.dict(
                os.environ, {"ANTHROPIC_API_KEY": "test"}, clear=False
            ), patch(
                "shadow_single_pass_editor.anthropic.Anthropic",
                return_value=client,
            ):
                with self.assertRaisesRegex(RuntimeError, "incomplete"):
                    run_shadow(job_dir=job_dir, model="claude-opus-5")
            report = json.loads(
                (
                    job_dir
                    / "shadow_single_pass_claude-opus-5.json"
                ).read_text(encoding="utf-8")
            )
            self.assertFalse(report["complete"])
            self.assertTrue(
                (
                    job_dir
                    / "shadow_single_pass_claude-opus-5_incomplete.txt"
                ).is_file()
            )


if __name__ == "__main__":
    unittest.main()
