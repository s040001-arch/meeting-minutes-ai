"""Tests for QUESTION_MODE light resume + coherence recognition_batch (no API)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from recognition_batch import (
    RECOGNITION_BATCH_FORMAT,
    build_batch_items,
    build_batch_question_text,
)
from run_question_cycle_once import (
    _build_coherence_single_question_payload,
    _mark_unknown_point_asked,
)
from recorrect_from_line_answer import _mark_batch_items_answered_in_unknowns


class CoherenceBatchWhenPausedTests(unittest.TestCase):
    def test_batch_question_shows_context_highlight_and_candidate(self) -> None:
        # 現在の逐語録（リング不足は既にリンク不足へ変化している）
        transcript = (
            "そういったところ、ちょっと人員のリンク不足みたいなところがなかなか払拭できてたり。"
            "金額も大きいので、ちゃんと役にさんの倫理決済みたいな、個人だから。"
        )
        points = [
            {
                "type": "coherence_review",
                "anomaly_id": "ta_001",
                "anomaly_word": "リング不足",
                "text": "リング不足",
                "span_text": "人材のリング不足みたいなところをどう埋めるか",
                "estimated_correction": "リソース不足",
                "confidence": "medium",
                "context_position_in_transcript": 15,
            },
            {
                "type": "coherence_review",
                "anomaly_id": "ta_002",
                "anomaly_word": "倫理決済",
                "text": "倫理決済",
                "span_text": "役員さんの倫理決済みたいなプロセスが必要",
                "estimated_correction": "稟議決裁",
                "confidence": "medium",
                "context_position_in_transcript": 50,
            },
        ]
        items = build_batch_items(points, full_text=transcript)
        text = build_batch_question_text(items)
        # 古い「リング不足」ではなく、今の逐語録の「リンク不足」を聞く
        self.assertIn("【リンク不足】", text)
        self.assertIn("→「リソース不足」？", text)
        self.assertIn("【倫理決済】", text)
        self.assertIn("→「稟議決裁」？", text)
        self.assertIn("人員の", text)
        self.assertIn("文字起こし原文", text)
        self.assertIn("要約・決定事項", text)
        self.assertNotIn("ドライマンゴー", text)
        # apply 対象は現在の surface
        self.assertEqual(items[0]["word"], "リンク不足")
        self.assertEqual(items[0]["detected_word"], "リング不足")

    def test_batch_payload_when_question_mode_line(self) -> None:
        pending = [
            {
                "type": "coherence_review",
                "source": "coherence_review",
                "anomaly_id": "ta_001",
                "anomaly_word": "リング不足",
                "text": "リング不足",
                "confidence": "medium",
                "estimated_correction": "リンク不足",
                "context_position_in_transcript": 10,
            },
            {
                "type": "coherence_review",
                "source": "coherence_review",
                "anomaly_id": "ta_002",
                "anomaly_word": "倫理決済",
                "text": "倫理決済",
                "confidence": "medium",
                "estimated_correction": "稟議決裁",
                "context_position_in_transcript": 20,
            },
            {
                "type": "coherence_review",
                "source": "coherence_review",
                "anomaly_id": "ta_003",
                "anomaly_word": "会議祭",
                "text": "会議祭",
                "confidence": "low",
                "estimated_correction": "",
                "context_position_in_transcript": 30,
            },
        ]
        with patch("question_mode.should_pause_for_answers", return_value=True):
            with patch(
                "run_question_cycle_once.write_line_pending_context"
            ) as mock_ctx:
                payload = _build_coherence_single_question_payload(
                    job_id="job_x",
                    coherence_pending=pending,
                    pending_meta={"pending_unknown_points_count": 3},
                    doc_url="",
                )
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["question_format"], RECOGNITION_BATCH_FORMAT)
        self.assertEqual(payload["question_status"], "generated")
        items = payload["selected_unknown"]["batch_items"]
        self.assertEqual(len(items), 3)
        self.assertIn("リング不足", payload["question_text"])
        self.assertIn("倫理決済", payload["question_text"])
        mock_ctx.assert_called_once()

    def test_fifo_single_when_question_mode_off(self) -> None:
        pending = [
            {
                "type": "coherence_review",
                "source": "coherence_review",
                "anomaly_id": "ta_001",
                "anomaly_word": "リング不足",
                "text": "リング不足",
                "confidence": "medium",
                "estimated_correction": "リンク不足",
                "context_position_in_transcript": 10,
            },
            {
                "type": "coherence_review",
                "source": "coherence_review",
                "anomaly_id": "ta_002",
                "anomaly_word": "倫理決済",
                "text": "倫理決済",
                "confidence": "medium",
                "estimated_correction": "稟議決裁",
                "context_position_in_transcript": 20,
            },
        ]
        with patch("question_mode.should_pause_for_answers", return_value=False):
            with patch("run_question_cycle_once.write_line_pending_context"):
                payload = _build_coherence_single_question_payload(
                    job_id="job_x",
                    coherence_pending=pending,
                    pending_meta={},
                    doc_url="",
                )
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["question_format"], "free_text")
        self.assertEqual(
            payload["selected_unknown"]["anomaly_id"], "ta_001"
        )
        self.assertNotIn("batch_items", payload["selected_unknown"])


class MarkBatchAskedAndAnsweredTests(unittest.TestCase):
    def test_mark_asked_covers_all_batch_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            unknowns = Path(tmp) / "unknown_points.json"
            points = [
                {
                    "type": "coherence_review",
                    "anomaly_id": "ta_001",
                    "anomaly_word": "リング不足",
                    "text": "リング不足",
                    "status": "open",
                },
                {
                    "type": "coherence_review",
                    "anomaly_id": "ta_002",
                    "anomaly_word": "倫理決済",
                    "text": "倫理決済",
                    "status": "open",
                },
            ]
            unknowns.write_text(
                json.dumps(points, ensure_ascii=False), encoding="utf-8"
            )
            selected = {
                "type": "recognition_batch",
                "batch_items": build_batch_items(points),
                "anomaly_id": "ta_001",
                "text": "リング不足",
            }
            n = _mark_unknown_point_asked(
                str(unknowns), selected, question_id="q1"
            )
            self.assertEqual(n, 2)
            data = json.loads(unknowns.read_text(encoding="utf-8"))
            self.assertTrue(all(x["status"] == "asked" for x in data))

    def test_unresolved_batch_items_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            job_id = "job_x"
            root = str(job)
            # layout: input_root/job_id/unknown_points.json
            jdir = job / job_id
            jdir.mkdir()
            points = [
                {
                    "type": "coherence_review",
                    "anomaly_id": "ta_001",
                    "anomaly_word": "リング不足",
                    "status": "asked",
                    "asked_by_question_id": "q1",
                },
                {
                    "type": "coherence_review",
                    "anomaly_id": "ta_002",
                    "anomaly_word": "倫理決済",
                    "status": "asked",
                    "asked_by_question_id": "q1",
                },
            ]
            (jdir / "unknown_points.json").write_text(
                json.dumps(points, ensure_ascii=False), encoding="utf-8"
            )
            batch_items = [
                {"anomaly_id": "ta_001", "word": "リング不足"},
                {"anomaly_id": "ta_002", "word": "倫理決済"},
            ]
            parsed = [
                {"anomaly_id": "ta_001", "word": "リング不足", "action": "correct"},
                {"anomaly_id": "ta_002", "word": "倫理決済", "action": "unknown"},
            ]
            n = _mark_batch_items_answered_in_unknowns(
                job_id=job_id,
                input_root=root,
                parsed=parsed,
                answer_text="1 リンク不足 / 2 不明",
                question_id="q1",
                batch_items=batch_items,
            )
            self.assertEqual(n, 2)
            data = json.loads(
                (jdir / "unknown_points.json").read_text(encoding="utf-8")
            )
            by_id = {x["anomaly_id"]: x for x in data}
            self.assertEqual(by_id["ta_001"]["status"], "answered")
            self.assertEqual(by_id["ta_002"]["status"], "open")


class AnswerLightCompletionAfterNoQuestionTests(unittest.TestCase):
    def test_question_cycle_generated_detects_status(self) -> None:
        from run_answer_light import _question_cycle_generated_new_question

        with tempfile.TemporaryDirectory() as d:
            job = Path(d)
            (job / "question_result.json").write_text(
                json.dumps({"question_status": "generated"}), encoding="utf-8"
            )
            self.assertTrue(_question_cycle_generated_new_question(job))
            (job / "question_result.json").write_text(
                json.dumps(
                    {
                        "question_status": "none",
                        "message": "proposal_impact=6 が閾値 7 未満",
                    }
                ),
                encoding="utf-8",
            )
            self.assertFalse(_question_cycle_generated_new_question(job))


class BatchAnswerParseTests(unittest.TestCase):
    """バッチ回答の番号分解（=区切り・削除指示）。"""

    def test_equals_format_delete_and_correct(self) -> None:
        from recognition_batch import apply_batch_corrections, parse_batch_answer

        answer = "1=削除、2=相原さん、3=削除、4=整理できたり、5=研修、6=削除、7=削除"
        items = [
            {"anomaly_id": "a1", "word": "誠実から進める"},
            {"anomaly_id": "a2", "word": "愛ナさん"},
            {"anomaly_id": "a3", "word": "コロナベンダー"},
            {"anomaly_id": "a4", "word": "作品できたり"},
            {"anomaly_id": "a5", "word": "医学部さん研修"},
            {"anomaly_id": "a6", "word": "農業形式"},
            {"anomaly_id": "a7", "word": "真っ白を通して"},
        ]
        parsed = parse_batch_answer(answer_text=answer, items=items, api_key=None)
        by_word = {p["word"]: p for p in parsed}
        self.assertEqual(by_word["誠実から進める"]["action"], "delete")
        self.assertEqual(by_word["愛ナさん"]["action"], "correct")
        self.assertEqual(by_word["愛ナさん"]["correction"], "相原さん")
        self.assertEqual(by_word["コロナベンダー"]["action"], "delete")
        self.assertEqual(by_word["作品できたり"]["correction"], "整理できたり")
        self.assertEqual(by_word["医学部さん研修"]["correction"], "研修")
        self.assertEqual(by_word["農業形式"]["action"], "delete")
        self.assertEqual(by_word["真っ白を通して"]["action"], "delete")
        # 本文に「削除」literal が入らないこと
        body = (
            "あの誠実から進める等々でご相談。先日さんの愛ナさんにメール。"
            "コロナベンダーサポート。作品できたり。医学部さん研修。"
            "農業形式で。真っ白を通していこうか。"
        )
        out, applied = apply_batch_corrections(body, parsed)
        self.assertNotIn("削除サポート", out)
        self.assertNotIn("あの削除", out)
        self.assertIn("相原さん", out)
        self.assertIn("整理できたり", out)
        self.assertIn("研修", out)
        delete_actions = [a for a in applied if a.get("action") == "delete"]
        self.assertGreaterEqual(len(delete_actions), 1)

    def test_coerce_delete_literal_correction(self) -> None:
        from recognition_batch import _coerce_parsed_row_action

        action, corr = _coerce_parsed_row_action(
            "correct", "削除", word="誠実から進める"
        )
        self.assertEqual(action, "delete")
        self.assertEqual(corr, "")


class SmartDeleteTests(unittest.TestCase):
    """delete のスマート削除: 断片のみ最小除去 + 削除のみ検証。"""

    def test_deletion_only_edit_validator(self) -> None:
        from recognition_batch import _is_deletion_only_edit

        orig = "っていうところで、あの誠実から進める等々でご相談をさせていただいてた。"
        # 純粋な削除 → OK
        self.assertTrue(
            _is_deletion_only_edit(orig, "っていうところで、ご相談をさせていただいてた。")
        )
        # 言い換え（挿入を含む）→ NG
        self.assertFalse(
            _is_deletion_only_edit(orig, "っていうところで、正式にご相談をさせていただいてた。")
        )
        # 全文同一 → NG（変更なしは呼び出し側で不採用）
        self.assertFalse(_is_deletion_only_edit(orig, orig))
        # 空 → NG
        self.assertFalse(_is_deletion_only_edit(orig, ""))

    def test_smart_delete_used_when_llm_returns_valid_deletion(self) -> None:
        from recognition_batch import apply_batch_corrections

        body = (
            "それの今後あの運用の取り回しについてどうでしょうか?"
            "っていうところで、あの誠実から進める等々でご相談をさせていただいてたので、"
            "そちらのすり合わせをさせていただきたいと思います。"
        )
        smart = (
            "っていうところで、ご相談をさせていただいてたので、"
            "そちらのすり合わせをさせていただきたいと思います。"
        )
        parsed = [
            {"anomaly_id": "a1", "word": "誠実から進める", "action": "delete", "correction": ""}
        ]
        with patch(
            "recognition_batch._smart_delete_sentence_via_llm", return_value=smart
        ):
            out, applied = apply_batch_corrections(body, parsed, api_key="test-key")
        self.assertNotIn("誠実から進める", out)
        # 実発言（すり合わせ）は残っている
        self.assertIn("すり合わせをさせていただきたい", out)
        self.assertIn("ご相談をさせていただいてた", out)
        self.assertEqual(applied[0]["mode"], "smart_fragment")

    def test_fallback_to_span_delete_without_api_key(self) -> None:
        from recognition_batch import apply_batch_corrections

        body = "前の文です。あの誠実から進める等々でご相談をさせていただいてた。次の文です。"
        parsed = [
            {"anomaly_id": "a1", "word": "誠実から進める", "action": "delete", "correction": ""}
        ]
        out, applied = apply_batch_corrections(body, parsed)
        self.assertNotIn("誠実から進める", out)
        self.assertIn("前の文です。", out)
        self.assertIn("次の文です。", out)


class WebhookLightResumeBranchTests(unittest.TestCase):
    def test_light_path_when_question_mode_line(self) -> None:
        from webhook_app import maybe_launch_auto_after_answer

        with patch.dict(os.environ, {"QUESTION_MODE": "line", "AUTO_AFTER_ANSWER": "1"}):
            with patch("webhook_app._try_acquire_auto_after_answer_lock", return_value=None):
                # lock fails → no launch, but we can inspect the branch via launch_label path
                with patch("webhook_app._launch_resume_subprocess") as mock_launch:
                    maybe_launch_auto_after_answer("job_x", save_ok=True)
                    mock_launch.assert_called_once()
                    kwargs = mock_launch.call_args.kwargs
                    self.assertEqual(kwargs["launch_label"], "auto_after_answer_light")
                    self.assertIn("run_answer_light.py", kwargs["cmd"][1])

    def test_legacy_path_when_question_mode_off(self) -> None:
        from webhook_app import maybe_launch_auto_after_answer

        with patch.dict(os.environ, {"QUESTION_MODE": "off", "AUTO_AFTER_ANSWER": "1"}, clear=False):
            os.environ.pop("QUESTION_MODE", None)
            with patch.dict(os.environ, {"QUESTION_MODE": "off"}):
                with patch("webhook_app._launch_resume_subprocess") as mock_launch:
                    maybe_launch_auto_after_answer("job_x", save_ok=True)
                    mock_launch.assert_called_once()
                    kwargs = mock_launch.call_args.kwargs
                    self.assertEqual(kwargs["launch_label"], "auto_after_answer")
                    self.assertIn("run_docs_hub_e2e.py", kwargs["cmd"][1])


class PrefillRemovedTests(unittest.TestCase):
    def test_knowledge_sheet_store_no_assistant_prefill(self) -> None:
        import inspect
        import knowledge_sheet_store as kss

        src = inspect.getsource(kss._merge_knowledge_memos_with_claude)
        self.assertNotIn('"role": "assistant"', src)
        src2 = inspect.getsource(kss._merge_knowledge_memos_with_all_answers)
        self.assertNotIn('"role": "assistant"', src2)


class TwoCharGateRelaxationTests(unittest.TestCase):
    def test_gate_allows_2char_when_candidate_and_located(self) -> None:
        from recognition_batch import is_valid_coherence_question_word as v

        # 候補あり＋位置特定済み → 2字許可
        self.assertTrue(v("決裁", has_candidate=True, located=True))
        self.assertTrue(v("同期", has_candidate=True, located=True))
        # 候補なし or 位置不明 → 従来どおり除外
        self.assertFalse(v("決裁"))
        self.assertFalse(v("決裁", has_candidate=True, located=False))
        self.assertFalse(v("決裁", has_candidate=False, located=True))
        # 1字は候補ありでも不可
        self.assertFalse(v("裁", has_candidate=True, located=True))
        # 3字以上は従来どおり無条件許可
        self.assertTrue(v("あやさん"))

    def test_build_batch_items_includes_2char_candidate_word(self) -> None:
        transcript = (
            "契約締結に向けて、稟議の決済を受けなければいけない事項を洗い出して、"
            "それらを合意するタイミングを最初に双方で認識しておく。"
        )
        pos = transcript.find("決済")
        points = [
            {
                "type": "coherence_review",
                "anomaly_id": "two_001",
                "anomaly_word": "決済",
                "text": "決済",
                "estimated_correction": "決裁",
                "confidence": "medium",
                "context_position_in_transcript": pos,
            }
        ]
        items = build_batch_items(points, full_text=transcript)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["word"], "決済")
        self.assertEqual(items[0]["estimated_correction"], "決裁")

    def test_build_batch_items_drops_2char_without_candidate(self) -> None:
        transcript = "契約締結に向けて、稟議の決済を受ける。"
        points = [
            {
                "type": "coherence_review",
                "anomaly_id": "two_002",
                "anomaly_word": "決済",
                "text": "決済",
                "estimated_correction": "",
                "confidence": "medium",
                "context_position_in_transcript": transcript.find("決済"),
            }
        ]
        items = build_batch_items(points, full_text=transcript)
        self.assertEqual(len(items), 0)

    def test_force_question_bypasses_gate_for_2char_no_candidate(self) -> None:
        transcript = "お名前をご存じか分からないんですが、暑さというものなんですけど。"
        points = [
            {
                "type": "coherence_review",
                "anomaly_id": "two_003",
                "anomaly_word": "暑さ",
                "text": "暑さ",
                "estimated_correction": "",
                "confidence": "medium",
                "context_position_in_transcript": transcript.find("暑さ"),
                "force_question": True,
            }
        ]
        items = build_batch_items(points, full_text=transcript)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["word"], "暑さ")

    def test_ok_with_candidate_means_accept_correction(self) -> None:
        from recognition_batch import apply_batch_corrections, parse_batch_answer

        transcript = "今、あやさんと川口の方でお話いただいてた。"
        items = [
            {
                "anomaly_id": "x1",
                "word": "あやさん",
                "estimated_correction": "相原さん",
                "context": "あやさん",
            }
        ]
        parsed = parse_batch_answer(answer_text="1 OK", items=items, api_key=None)
        self.assertEqual(parsed[0]["action"], "correct")
        self.assertEqual(parsed[0]["correction"], "相原さん")
        updated, applied = apply_batch_corrections(transcript, parsed)
        self.assertIn("相原さん", updated)
        self.assertNotIn("あやさん", updated)
        self.assertEqual(len(applied), 1)


class ReenterCompletedJobTests(unittest.TestCase):
    def test_inject_locates_gates_and_is_idempotent(self) -> None:
        import reenter_completed_job as rc

        transcript = (
            "お名前をご存じか分からないんですが、暑さというものなんですけど。"
            "稟議の決済を受けなければいけない。今、あやさんと川口の方で。"
        )
        # located 2char with candidate -> ok
        p1 = rc._build_injected_point(transcript, {"word": "決済", "correction": "決裁"})
        self.assertIsNotNone(p1)
        self.assertEqual(p1["anomaly_word"], "決済")
        self.assertEqual(p1["status"], "open")
        self.assertGreaterEqual(p1["context_position_in_transcript"], 0)
        # 3char name-ish located, no candidate -> still ok (>=3)
        p2 = rc._build_injected_point(transcript, {"word": "あやさん", "correction": "相原さん"})
        self.assertIsNotNone(p2)
        # not found in transcript -> skipped
        p3 = rc._build_injected_point(transcript, {"word": "存在しない語", "correction": "x"})
        self.assertIsNone(p3)
        # 2char without candidate -> gate drop
        p4 = rc._build_injected_point(transcript, {"word": "暑さ", "correction": ""})
        self.assertIsNone(p4)
        # 2char without candidate but force=true -> injected
        p4f = rc._build_injected_point(
            transcript, {"word": "暑さ", "correction": "", "force": True}
        )
        self.assertIsNotNone(p4f)
        self.assertTrue(p4f["force_question"])
        # stable id: same word+pos -> same id
        p1b = rc._build_injected_point(transcript, {"word": "決済", "correction": "決裁"})
        self.assertEqual(p1["anomaly_id"], p1b["anomaly_id"])

    def test_merge_unknowns_idempotent(self) -> None:
        import reenter_completed_job as rc

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "unknown_points.json"
            pts = [
                {"anomaly_id": "reentry_aaa", "anomaly_word": "決済", "status": "open"},
            ]
            added, total = rc._merge_unknowns(path, pts)
            self.assertEqual(added, 1)
            self.assertEqual(total, 1)
            # second time -> no dup
            added2, total2 = rc._merge_unknowns(path, pts)
            self.assertEqual(added2, 0)
            self.assertEqual(total2, 1)


class SpanHypothesisQuestionTests(unittest.TestCase):
    """崩壊文スパンを仮説付きで確認する質問タイプ(span_hypothesis)。"""

    TRANSCRIPT = (
        "はい、よろしくお願いします。"
        "ドネと協定を狩ることは廃止できるんじゃないかなと思ってまして。"
        "その他はこれまで通りで進めます。"
    )
    SPAN = "ドネと協定を狩ることは廃止できるんじゃないかな"
    HYPO = "NEST上の協定の確認は廃止できるんじゃないかな"

    def _point(self) -> dict:
        return {
            "type": "coherence_review",
            "anomaly_id": "span_001",
            "anomaly_word": self.SPAN,
            "text": self.SPAN,
            "span_text": self.SPAN,
            "span_corrected": self.HYPO,
            "estimated_correction": self.HYPO,
            "confidence": "medium",
            "anomaly_type": "C",
            "question_kind": "span_hypothesis",
            "context_position_in_transcript": self.TRANSCRIPT.find(self.SPAN),
            "force_question": True,
            "status": "open",
        }

    def test_build_batch_items_produces_span_item(self) -> None:
        items = build_batch_items([self._point()], full_text=self.TRANSCRIPT)
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["question_kind"], "span_hypothesis")
        self.assertEqual(it["word"], self.SPAN)
        self.assertEqual(it["estimated_correction"], self.HYPO)
        self.assertTrue(it["found_in_transcript"])

    def test_question_text_shows_quote_and_hypothesis(self) -> None:
        items = build_batch_items([self._point()], full_text=self.TRANSCRIPT)
        text = build_batch_question_text(items)
        self.assertIn("意味が取りにくい発言", text)
        self.assertIn(self.SPAN, text)
        self.assertIn(f"仮説:「{self.HYPO}」", text)
        self.assertIn("という趣旨でしょうか", text)

    def test_auto_type_c_with_span_corrected_becomes_span_item(self) -> None:
        # 自動検出(C)でも span_corrected 仮説があれば文単位の質問になる
        point = self._point()
        point.pop("question_kind")
        point.pop("force_question")
        point["anomaly_word"] = "ドネと協定"
        items = build_batch_items([point], full_text=self.TRANSCRIPT)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["question_kind"], "span_hypothesis")
        self.assertEqual(items[0]["word"], self.SPAN)

    def test_ok_answer_adopts_hypothesis(self) -> None:
        from recognition_batch import apply_batch_corrections, parse_batch_answer

        items = build_batch_items([self._point()], full_text=self.TRANSCRIPT)
        parsed = parse_batch_answer(answer_text="1 OK", items=items, api_key=None)
        self.assertEqual(parsed[0]["action"], "correct")
        self.assertEqual(parsed[0]["correction"], self.HYPO)
        updated, applied = apply_batch_corrections(self.TRANSCRIPT, parsed)
        self.assertIn(self.HYPO, updated)
        self.assertNotIn("ドネと協定", updated)
        self.assertEqual(len(applied), 1)

    def test_freetext_rewording_replaces_span(self) -> None:
        from recognition_batch import apply_batch_corrections, parse_batch_answer

        reword = "NEST上の協定確認の工程は廃止できるんじゃないかな"
        parsed = parse_batch_answer(
            answer_text=f"1 「{reword}」",
            items=build_batch_items([self._point()], full_text=self.TRANSCRIPT),
            api_key=None,
        )
        self.assertEqual(parsed[0]["action"], "correct")
        self.assertEqual(parsed[0]["correction"], reword)
        updated, _ = apply_batch_corrections(self.TRANSCRIPT, parsed)
        self.assertIn(reword, updated)
        self.assertNotIn("ドネと協定", updated)

    def test_delete_answer_removes_span(self) -> None:
        from recognition_batch import apply_batch_corrections, parse_batch_answer

        items = build_batch_items([self._point()], full_text=self.TRANSCRIPT)
        parsed = parse_batch_answer(answer_text="1 削除", items=items, api_key=None)
        self.assertEqual(parsed[0]["action"], "delete")
        updated, _ = apply_batch_corrections(self.TRANSCRIPT, parsed)
        self.assertNotIn("ドネと協定", updated)
        self.assertIn("よろしくお願いします", updated)
        self.assertIn("これまで通り", updated)

    def test_reenter_builds_span_hypothesis_point(self) -> None:
        import reenter_completed_job as rc

        spec = {"span": self.SPAN, "hypothesis": self.HYPO, "reason": "文崩壊"}
        p = rc._build_injected_point(self.TRANSCRIPT, spec)
        self.assertIsNotNone(p)
        self.assertEqual(p["question_kind"], "span_hypothesis")
        self.assertEqual(p["span_text"], self.SPAN)
        self.assertEqual(p["span_corrected"], self.HYPO)
        self.assertTrue(p["force_question"])
        # span が逐語録に無い場合はスキップ（陳腐化防止）
        p_missing = rc._build_injected_point(
            self.TRANSCRIPT, {"span": "存在しないスパンです", "hypothesis": "x"}
        )
        self.assertIsNone(p_missing)

    def test_stale_span_falls_back_without_crash(self) -> None:
        # 逐語録が変わって span が見つからない -> 従来 word モードへフォールバック
        point = self._point()
        point["span_text"] = "もう存在しない古いスパン"
        point["anomaly_word"] = "もう存在しない古いスパン"
        items = build_batch_items([point], full_text=self.TRANSCRIPT)
        # word も逐語録に無いので通常経路で処理される（クラッシュしない）
        self.assertIsInstance(items, list)


if __name__ == "__main__":
    unittest.main()
