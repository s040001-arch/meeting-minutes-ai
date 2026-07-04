"""Tests for reader_pass.py (Step 4 pipeline integration)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from reader_pass import (
    READER_PASS_MODEL,
    READER_PASS_QUESTIONS_MD,
    READER_PASS_RESULT,
    build_questions_md,
    is_enabled,
    load_exclusion_items,
    parse_findings,
    run_reader_pass,
)


# ---------------------------------------------------------------------------
# parse_findings
# ---------------------------------------------------------------------------

class ParseFindingsTests(unittest.TestCase):
    def test_clean_json_array(self) -> None:
        raw = json.dumps([
            {"rank": 1, "excerpt": "テスト", "reason": "理由", "question": "質問？"}
        ])
        findings = parse_findings(raw)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["rank"], 1)
        self.assertEqual(findings[0]["excerpt"], "テスト")

    def test_strips_code_block(self) -> None:
        raw = "```json\n[{\"rank\":1,\"excerpt\":\"X\",\"reason\":\"R\",\"question\":\"Q?\"}]\n```"
        findings = parse_findings(raw)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["excerpt"], "X")

    def test_preamble_before_array(self) -> None:
        raw = "以下の通りです。\n[{\"rank\":1,\"excerpt\":\"A\",\"reason\":\"B\",\"question\":\"C?\"}]"
        findings = parse_findings(raw)
        self.assertEqual(len(findings), 1)

    def test_empty_on_invalid_json(self) -> None:
        self.assertEqual(parse_findings("not json at all"), [])

    def test_empty_on_no_array(self) -> None:
        self.assertEqual(parse_findings('{"rank": 1}'), [])

    def test_five_items(self) -> None:
        items = [
            {"rank": i, "excerpt": f"e{i}", "reason": f"r{i}", "question": f"q{i}?"}
            for i in range(1, 6)
        ]
        findings = parse_findings(json.dumps(items))
        self.assertEqual(len(findings), 5)


# ---------------------------------------------------------------------------
# load_exclusion_items
# ---------------------------------------------------------------------------

class LoadExclusionItemsTests(unittest.TestCase):
    def _make_proposals(self, tmp: Path, proposals: list[dict]) -> Path:
        p = tmp / "edit_proposals.json"
        p.write_text(json.dumps({"proposals": proposals}), encoding="utf-8")
        return p

    def _make_unknowns(self, tmp: Path, unknowns: list[dict]) -> None:
        (tmp / "unknown_points.json").write_text(
            json.dumps(unknowns), encoding="utf-8"
        )

    def test_extracts_ask_verdicts(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self._make_proposals(tmp, [
                {"verdict": "ask_with_candidate", "anomaly_word": "ターゲット語", "span_before": ""},
                {"verdict": "auto_delete", "anomaly_word": "除外されない", "span_before": ""},
                {"verdict": "ask_without_candidate", "anomaly_word": "", "span_before": "文脈前"},
            ])
            items = load_exclusion_items(tmp)
        self.assertIn("ターゲット語", items)
        self.assertIn("文脈前", items)
        self.assertNotIn("除外されない", items)

    def test_skips_answered_unknown_points(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self._make_unknowns(tmp, [
                {"status": "open", "anomaly_word": "open語"},
                {"status": "answered", "anomaly_word": "answered語"},
            ])
            items = load_exclusion_items(tmp)
        self.assertIn("open語", items)
        self.assertNotIn("answered語", items)

    def test_explicit_proposals_path(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            explicit = tmp / "custom_proposals.json"
            explicit.write_text(json.dumps({"proposals": [
                {"verdict": "ask_with_candidate", "anomaly_word": "カスタム語", "span_before": ""}
            ]}), encoding="utf-8")
            items = load_exclusion_items(tmp, proposals_path=explicit)
        self.assertIn("カスタム語", items)

    def test_missing_files_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            items = load_exclusion_items(Path(d))
        self.assertEqual(items, [])

    def test_malformed_json_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "edit_proposals.json").write_text("not json", encoding="utf-8")
            items = load_exclusion_items(tmp)
        self.assertEqual(items, [])


# ---------------------------------------------------------------------------
# build_questions_md
# ---------------------------------------------------------------------------

class BuildQuestionsMdTests(unittest.TestCase):
    SAMPLE = [
        {"rank": 1, "excerpt": "テキスト1", "reason": "理由1", "question": "質問1？"},
        {"rank": 2, "excerpt": "テキスト2", "reason": "理由2", "question": "質問2？"},
    ]

    def test_contains_all_excerpts(self) -> None:
        md = build_questions_md(self.SAMPLE)
        self.assertIn("テキスト1", md)
        self.assertIn("テキスト2", md)

    def test_contains_answer_slot(self) -> None:
        md = build_questions_md(self.SAMPLE)
        self.assertGreaterEqual(md.count("→ 回答:"), 2)

    def test_contains_rank_headers(self) -> None:
        md = build_questions_md(self.SAMPLE)
        self.assertIn("## #1", md)
        self.assertIn("## #2", md)

    def test_job_id_in_header(self) -> None:
        md = build_questions_md(self.SAMPLE, job_id="job_20260701_053826")
        self.assertIn("job_20260701_053826", md)

    def test_model_in_footer(self) -> None:
        md = build_questions_md(self.SAMPLE)
        self.assertIn(READER_PASS_MODEL, md)


# ---------------------------------------------------------------------------
# is_enabled
# ---------------------------------------------------------------------------

class IsEnabledTests(unittest.TestCase):
    def test_off_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("READER_PASS_ENABLED", None)
            self.assertFalse(is_enabled())

    def test_on_when_set(self) -> None:
        with patch.dict(os.environ, {"READER_PASS_ENABLED": "on"}):
            self.assertTrue(is_enabled())

    def test_case_insensitive(self) -> None:
        with patch.dict(os.environ, {"READER_PASS_ENABLED": "ON"}):
            self.assertTrue(is_enabled())

    def test_off_when_other_value(self) -> None:
        with patch.dict(os.environ, {"READER_PASS_ENABLED": "yes"}):
            self.assertFalse(is_enabled())


# ---------------------------------------------------------------------------
# run_reader_pass (モック)
# ---------------------------------------------------------------------------

_SAMPLE_FINDINGS = [
    {"rank": i, "excerpt": f"テキスト{i}", "reason": f"理由{i}", "question": f"質問{i}？"}
    for i in range(1, 6)
]


def _make_mock_message(text: str) -> MagicMock:
    msg = MagicMock()
    msg.model = READER_PASS_MODEL
    msg.usage.input_tokens = 100
    msg.usage.output_tokens = 200
    msg.stop_reason = "end_turn"
    msg.content = [MagicMock(text=text)]
    return msg


class RunReaderPassTests(unittest.TestCase):
    def _setup_job_dir(self, tmp: Path, transcript: str = "会議内容") -> None:
        (tmp / "merged_transcript_ai.txt").write_text(transcript, encoding="utf-8")

    @patch("reader_pass.anthropic.Anthropic")
    def test_writes_result_json(self, mock_anthropic_cls) -> None:
        mock_anthropic_cls.return_value.messages.create.return_value = (
            _make_mock_message(json.dumps(_SAMPLE_FINDINGS))
        )
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self._setup_job_dir(tmp)
            run_reader_pass(tmp)
            result_path = tmp / READER_PASS_RESULT
            self.assertTrue(result_path.exists())
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(len(result["findings"]), 5)
            self.assertEqual(result["model"], READER_PASS_MODEL)

    @patch("reader_pass.anthropic.Anthropic")
    def test_writes_questions_md(self, mock_anthropic_cls) -> None:
        mock_anthropic_cls.return_value.messages.create.return_value = (
            _make_mock_message(json.dumps(_SAMPLE_FINDINGS))
        )
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self._setup_job_dir(tmp)
            run_reader_pass(tmp)
            md_path = tmp / READER_PASS_QUESTIONS_MD
            self.assertTrue(md_path.exists())
            md = md_path.read_text(encoding="utf-8")
            self.assertIn("→ 回答:", md)
            self.assertIn("## #1", md)

    @patch("reader_pass.anthropic.Anthropic")
    def test_explicit_ai_txt_path(self, mock_anthropic_cls) -> None:
        mock_anthropic_cls.return_value.messages.create.return_value = (
            _make_mock_message(json.dumps(_SAMPLE_FINDINGS))
        )
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            custom_ai = tmp / "custom_ai.txt"
            custom_ai.write_text("カスタム逐語録", encoding="utf-8")
            run_reader_pass(tmp, ai_txt_path=custom_ai)
            # APIに渡されたメッセージにカスタム内容が含まれるか
            call_args = mock_anthropic_cls.return_value.messages.create.call_args
            user_content = call_args.kwargs["messages"][0]["content"]
            self.assertIn("カスタム逐語録", user_content)

    @patch("reader_pass.anthropic.Anthropic")
    def test_excluded_count_in_result(self, mock_anthropic_cls) -> None:
        mock_anthropic_cls.return_value.messages.create.return_value = (
            _make_mock_message(json.dumps(_SAMPLE_FINDINGS))
        )
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self._setup_job_dir(tmp)
            (tmp / "edit_proposals.json").write_text(json.dumps({"proposals": [
                {"verdict": "ask_with_candidate", "anomaly_word": "語A", "span_before": ""},
                {"verdict": "ask_without_candidate", "anomaly_word": "語B", "span_before": ""},
            ]}), encoding="utf-8")
            result = run_reader_pass(tmp)
            self.assertEqual(result["excluded_count"], 2)

    @patch("reader_pass.anthropic.Anthropic")
    def test_llm_parse_failure_yields_empty_findings(self, mock_anthropic_cls) -> None:
        mock_anthropic_cls.return_value.messages.create.return_value = (
            _make_mock_message("JSONではないテキスト")
        )
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self._setup_job_dir(tmp)
            result = run_reader_pass(tmp)
            self.assertEqual(result["findings"], [])
            # result.json と questions.md は書き込まれる
            self.assertTrue((tmp / READER_PASS_RESULT).exists())
            self.assertTrue((tmp / READER_PASS_QUESTIONS_MD).exists())


if __name__ == "__main__":
    unittest.main()
