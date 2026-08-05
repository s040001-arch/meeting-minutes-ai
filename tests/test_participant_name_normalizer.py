"""participant_name_normalizer のテスト。"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import participant_name_normalizer as pnn


def _fake_llm(replacement_map):
    """_confirm_with_llm を差し替えるためのヘルパー。"""

    def fake(candidates, participants, customer):
        return {
            base: right
            for base, right in replacement_map.items()
            if base in candidates
        }

    return fake


class ParticipantNormalizerTests(unittest.TestCase):
    def setUp(self):
        self.profile = {
            "customer_name": "楽天インサイト",
            "participants": ["山屋様", "相原"],
        }

    def test_replaces_confirmed_misrecognition(self):
        text = "山谷さんが説明した。山谷さんの資料。相原が回答。"
        with mock.patch.object(
            pnn, "_confirm_with_llm", side_effect=_fake_llm({"山谷": "山屋"})
        ):
            out, applied = pnn.normalize_participant_names(text, self.profile)
        self.assertNotIn("山谷", out)
        self.assertEqual(out.count("山屋さん"), 2)
        self.assertEqual(applied, [{"wrong": "山谷", "right": "山屋", "count": 2}])

    def test_replaces_bare_occurrences_too(self):
        text = "山谷さんの話。あれは山谷の担当です。"
        with mock.patch.object(
            pnn, "_confirm_with_llm", side_effect=_fake_llm({"山谷": "山屋"})
        ):
            out, applied = pnn.normalize_participant_names(text, self.profile)
        self.assertNotIn("山谷", out)
        self.assertEqual(applied[0]["count"], 2)

    def test_keep_verdict_leaves_text_unchanged(self):
        text = "山谷さんが説明した。"
        with mock.patch.object(pnn, "_confirm_with_llm", side_effect=_fake_llm({})):
            out, applied = pnn.normalize_participant_names(text, self.profile)
        self.assertEqual(out, text)
        self.assertEqual(applied, [])

    def test_correct_names_are_not_candidates(self):
        text = "山屋さんが説明した。相原さんが答えた。"
        with mock.patch.object(pnn, "_confirm_with_llm") as llm:
            out, applied = pnn.normalize_participant_names(text, self.profile)
        llm.assert_not_called()  # 候補ゼロなら LLM を呼ばない
        self.assertEqual(out, text)
        self.assertEqual(applied, [])

    def test_no_participants_is_noop(self):
        text = "山谷さんが説明した。"
        with mock.patch.object(pnn, "_confirm_with_llm") as llm:
            out, applied = pnn.normalize_participant_names(text, {})
        llm.assert_not_called()
        self.assertEqual(out, text)
        self.assertEqual(applied, [])

    def test_llm_failure_is_safe_noop(self):
        text = "山谷さんが説明した。"
        with mock.patch.object(
            pnn, "_confirm_with_llm", side_effect=RuntimeError("api down")
        ):
            out, applied = pnn.normalize_participant_names(text, self.profile)
        self.assertEqual(out, text)
        self.assertEqual(applied, [])

    def test_unrelated_names_not_sent_to_llm(self):
        # 参加者と似ていない人名（先頭文字も異なり類似度も低い）は候補外
        text = "田中さんが説明した。"
        with mock.patch.object(pnn, "_confirm_with_llm") as llm:
            pnn.normalize_participant_names(text, self.profile)
        llm.assert_not_called()

    def test_audit_file_written(self):
        text = "山谷さんが説明した。"
        with tempfile.TemporaryDirectory() as job_dir:
            with mock.patch.object(
                pnn, "_confirm_with_llm", side_effect=_fake_llm({"山谷": "山屋"})
            ):
                pnn.normalize_participant_names(
                    text, self.profile, job_dir=job_dir
                )
            path = os.path.join(job_dir, pnn.AUDIT_FILENAME)
            self.assertTrue(os.path.isfile(path))
            with open(path, encoding="utf-8") as handle:
                audit = json.load(handle)
        self.assertEqual(audit["applied"][0]["wrong"], "山谷")
        self.assertIn("山谷", audit["candidates"])

    def test_llm_creation_is_rejected(self):
        # LLM が参加者リストにない置換先を返しても採用しない
        text = "山谷さんが説明した。"
        fake_resp = mock.MagicMock()
        fake_resp.content = [
            mock.MagicMock(
                type="text",
                text='[{"index":1,"verdict":"replace","replace_with":"架空"}]',
            )
        ]
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}):
            with mock.patch("anthropic.Anthropic") as client_cls:
                client_cls.return_value.messages.create.return_value = fake_resp
                out, applied = pnn.normalize_participant_names(
                    text, self.profile
                )
        self.assertEqual(out, text)
        self.assertEqual(applied, [])


if __name__ == "__main__":
    unittest.main()
