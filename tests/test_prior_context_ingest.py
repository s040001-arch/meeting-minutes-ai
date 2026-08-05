"""prior_context_ingest のテスト。"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import prior_context_ingest as pci


class NoContextReplyTests(unittest.TestCase):
    def test_no_variants(self):
        for s in ["なし", "特になし。", "ないです", "ありません", "OK", "大丈夫です！"]:
            self.assertTrue(pci.is_no_context_reply(s), s)

    def test_real_content_is_not_no(self):
        self.assertFalse(pci.is_no_context_reply("山屋です。背景を共有します。"))


class PendingContextTests(unittest.TestCase):
    def test_write_and_clear_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "line_pending_context.json")
            with mock.patch.object(pci, "LINE_PENDING_CONTEXT_PATH", path):
                with mock.patch.dict(os.environ, {"LINE_PENDING_SYNC_URL": ""}):
                    pci.write_prior_context_pending("job_x")
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                self.assertEqual(data["kind"], pci.PENDING_KIND)
                self.assertEqual(data["job_id"], "job_x")
                pci.clear_prior_context_pending()
                self.assertFalse(os.path.isfile(path))

    def test_clear_does_not_remove_question_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "line_pending_context.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"job_id": "job_x", "question_id": "q1"}, f)
            with mock.patch.object(pci, "LINE_PENDING_CONTEXT_PATH", path):
                pci.clear_prior_context_pending()
            self.assertTrue(os.path.isfile(path))


class IngestTests(unittest.TestCase):
    def _run_ingest(self, tmp, memos, sheet_existing=None):
        job_id = "job_t"
        job_dir = os.path.join(tmp, "data", "transcriptions", job_id)
        os.makedirs(job_dir)
        with open(
            os.path.join(job_dir, "meeting_profile.json"), "w", encoding="utf-8"
        ) as f:
            json.dump({"customer_name": "楽天", "relevant_knowledge": ["既存メモ"]}, f)
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            fake_store = mock.MagicMock()
            fake_store.load_knowledge_memos.return_value = list(sheet_existing or [])
            with mock.patch.object(
                pci, "_extract_memos_with_llm", return_value=memos
            ):
                with mock.patch.dict(
                    sys.modules, {"knowledge_sheet_store": fake_store}
                ):
                    result = pci.ingest_prior_context("メール本文", job_id)
        finally:
            os.chdir(cwd)
        return result, job_dir, fake_store

    def test_ingest_updates_raw_sheet_and_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, job_dir, store = self._run_ingest(
                tmp, ["山屋さんは楽天の担当者", "既存メモ"]
            )
            self.assertTrue(result["raw_saved"])
            self.assertEqual(result["memos_extracted"], 2)
            # シートには新規1件のみ追加（既存メモは重複除外）
            store.save_knowledge_memos.assert_called_once()
            saved = store.save_knowledge_memos.call_args[0][0]
            self.assertEqual(saved, ["山屋さんは楽天の担当者", "既存メモ"])
            # ↑既存シートが空なので両方追加される
            with open(
                os.path.join(job_dir, "meeting_profile.json"), encoding="utf-8"
            ) as f:
                profile = json.load(f)
            self.assertEqual(
                profile["relevant_knowledge"], ["既存メモ", "山屋さんは楽天の担当者"]
            )
            self.assertTrue(
                os.path.isfile(os.path.join(job_dir, pci.PRIOR_CONTEXT_FILENAME))
            )

    def test_ingest_dedups_against_sheet(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, _, store = self._run_ingest(
                tmp, ["山屋さんは楽天の担当者"], sheet_existing=["山屋さんは楽天の担当者"]
            )
            self.assertEqual(result["sheet_added"], 0)
            store.save_knowledge_memos.assert_not_called()

    def test_llm_failure_still_saves_raw(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_id = "job_t"
            job_dir = os.path.join(tmp, "data", "transcriptions", job_id)
            os.makedirs(job_dir)
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with mock.patch.object(
                    pci, "_extract_memos_with_llm", return_value=[]
                ):
                    result = pci.ingest_prior_context("メール本文", job_id)
            finally:
                os.chdir(cwd)
            self.assertTrue(result["raw_saved"])
            self.assertEqual(result["memos_extracted"], 0)
            self.assertTrue(
                os.path.isfile(os.path.join(job_dir, pci.PRIOR_CONTEXT_FILENAME))
            )


if __name__ == "__main__":
    unittest.main()
