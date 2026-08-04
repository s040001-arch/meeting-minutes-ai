"""Tests for reprocess_job.py."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from reprocess_job import (
    _BACKUP_FILES,
    _STEP_ORDER,
    backup_job_dir,
    load_doc_id,
    reprocess,
    run_step,
    verify_doc_pattern,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_job_dir(tmp: Path, *, with_doc_hub: bool = True) -> Path:
    job = tmp / "job_test_123"
    job.mkdir()
    (job / "merged_transcript_after_qa.txt").write_text("after_qa", encoding="utf-8")
    (job / "minutes_draft.md").write_text("# draft", encoding="utf-8")
    (job / "minutes_structured.md").write_text("# structured", encoding="utf-8")
    if with_doc_hub:
        (job / "google_doc_hub.json").write_text(
            json.dumps({"doc_id": "DOC_ID_TEST", "doc_url": "https://example.com"}),
            encoding="utf-8",
        )
    return job


# ---------------------------------------------------------------------------
# backup_job_dir
# ---------------------------------------------------------------------------

class BackupTests(unittest.TestCase):
    def test_copies_existing_files(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            job = _make_job_dir(Path(d))
            backup = backup_job_dir(job)
            self.assertTrue(backup.exists())
            self.assertTrue((backup / "minutes_draft.md").exists())
            self.assertTrue((backup / "minutes_structured.md").exists())
            self.assertTrue((backup / "google_doc_hub.json").exists())
            self.assertTrue((backup / "merged_transcript_after_qa.txt").exists())

    def test_skips_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            job = Path(d) / "empty_job"
            job.mkdir()
            backup = backup_job_dir(job)
            self.assertTrue(backup.exists())
            for name in _BACKUP_FILES:
                self.assertFalse((backup / name).exists())

    def test_dry_run_does_not_create_dir(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            job = _make_job_dir(Path(d))
            backup = backup_job_dir(job, dry_run=True)
            self.assertFalse(backup.exists())

    def test_backup_dir_name_has_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            job = _make_job_dir(Path(d))
            backup = backup_job_dir(job)
            self.assertIn("_reprocess_backup_", backup.name)


# ---------------------------------------------------------------------------
# load_doc_id
# ---------------------------------------------------------------------------

class LoadDocIdTests(unittest.TestCase):
    def test_returns_doc_id(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            job = _make_job_dir(Path(d))
            self.assertEqual(load_doc_id(job), "DOC_ID_TEST")

    def test_returns_none_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            job = _make_job_dir(Path(d), with_doc_hub=False)
            self.assertIsNone(load_doc_id(job))

    def test_returns_none_on_malformed_json(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            job = _make_job_dir(Path(d), with_doc_hub=False)
            (job / "google_doc_hub.json").write_text("not json", encoding="utf-8")
            self.assertIsNone(load_doc_id(job))


# ---------------------------------------------------------------------------
# run_step
# ---------------------------------------------------------------------------

class RunStepTests(unittest.TestCase):
    def test_dry_run_returns_empty(self) -> None:
        result = run_step(["echo", "hi"], "step_test", dry_run=True)
        self.assertEqual(result, {})

    @patch("reprocess_job.subprocess.run")
    def test_parses_key_value_output(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="foo=bar\nbaz=qux\n", stderr="")
        result = run_step(["cmd"], "step_test")
        self.assertEqual(result["foo"], "bar")
        self.assertEqual(result["baz"], "qux")
        self.assertEqual(mock_run.call_args.kwargs["errors"], "replace")

    @patch("reprocess_job.subprocess.run")
    def test_tolerates_missing_captured_streams(self, mock_run) -> None:
        """子プロセス側のデコード障害等で stdout/stderr が None でも落ちない。"""
        mock_run.return_value = MagicMock(returncode=0, stdout=None, stderr=None)
        self.assertEqual(run_step(["cmd"], "step_test"), {})

    @patch("reprocess_job.subprocess.run")
    def test_raises_on_nonzero_exit(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error msg")
        with self.assertRaises(RuntimeError) as ctx:
            run_step(["cmd"], "step_test")
        self.assertIn("step_test", str(ctx.exception))


# ---------------------------------------------------------------------------
# reprocess – step filtering
# ---------------------------------------------------------------------------

class ReprocessStepFilterTests(unittest.TestCase):
    """from_step によって実行されるステップが変わることを確認する。"""

    def _run_reprocess(self, from_step: str, tmp: Path) -> list[str]:
        job = _make_job_dir(tmp)
        captured_cmds: list[str] = []

        def fake_run_step(cmd, step, *, dry_run=False):
            captured_cmds.append(step)
            return {"full_write_verified": "True"} if "export" in " ".join(cmd) else {}

        with patch("reprocess_job.run_step", side_effect=fake_run_step), \
             patch("reprocess_job.backup_job_dir", return_value=job / "_backup"), \
             patch("reprocess_job.verify_doc_pattern", return_value={"found": True, "count": 1, "doc_chars": 100, "pattern": "[補足:"}):
            reprocess(
                job,
                input_root=str(tmp),
                from_step=from_step,
                input_override=None,
                verify_pattern="[補足:",
                credentials="credentials.json",
                token="token.json",
                docs_chunk_size=5000,
                reason="test",
                dry_run=False,
            )
        return captured_cmds

    def test_from_step_6_1_runs_all(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            steps = self._run_reprocess("6.1", Path(d))
        self.assertIn("step_6_1", steps)
        self.assertIn("step_6_2", steps)
        self.assertIn("step_6_3", steps)

    def test_from_step_6_2_skips_6_1(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            steps = self._run_reprocess("6.2", Path(d))
        self.assertNotIn("step_6_1", steps)
        self.assertIn("step_6_2", steps)
        self.assertIn("step_6_3", steps)

    def test_from_step_6_3_runs_only_6_3(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            steps = self._run_reprocess("6.3", Path(d))
        self.assertNotIn("step_6_1", steps)
        self.assertNotIn("step_6_2", steps)
        self.assertIn("step_6_3", steps)


# ---------------------------------------------------------------------------
# reprocess – input_override
# ---------------------------------------------------------------------------

class ReprocessInputOverrideTests(unittest.TestCase):
    def _collect_cmds(self, from_step: str, tmp: Path, override_path: Path) -> list[list[str]]:
        job = _make_job_dir(tmp)
        captured: list[list[str]] = []

        def fake_run_step(cmd, step, *, dry_run=False):
            captured.append(list(cmd))
            return {"full_write_verified": "True"}

        with patch("reprocess_job.run_step", side_effect=fake_run_step), \
             patch("reprocess_job.backup_job_dir", return_value=job / "_backup"), \
             patch("reprocess_job.verify_doc_pattern", return_value={"found": True, "count": 1, "doc_chars": 100, "pattern": "[補足:"}):
            reprocess(
                job,
                input_root=str(tmp),
                from_step=from_step,
                input_override=override_path,
                verify_pattern="[補足:",
                credentials="credentials.json",
                token="token.json",
                docs_chunk_size=5000,
                reason="test",
                dry_run=False,
            )
        return captured

    def test_override_passed_to_step_6_1(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            override = tmp / "annotated.txt"
            override.write_text("annotated transcript", encoding="utf-8")
            cmds = self._collect_cmds("6.1", tmp, override)
        step61_cmd = next(c for c in cmds if "generate_minutes_transcript" in " ".join(c))
        self.assertIn("--input", step61_cmd)
        self.assertIn(str(override), step61_cmd)

    def test_override_passed_to_step_6_2(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            override = tmp / "draft.md"
            override.write_text("# override draft", encoding="utf-8")
            cmds = self._collect_cmds("6.2", tmp, override)
        step62_cmd = next(c for c in cmds if "generate_minutes_other_sections" in " ".join(c))
        self.assertIn("--input", step62_cmd)

    def test_override_not_passed_when_from_step_differs(self) -> None:
        """6.1 始まりでは 6.2 の --input には override が渡らない。"""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            override = tmp / "annotated.txt"
            override.write_text("text", encoding="utf-8")
            cmds = self._collect_cmds("6.1", tmp, override)
        step62_cmd = next(c for c in cmds if "generate_minutes_other_sections" in " ".join(c))
        self.assertNotIn("--input", step62_cmd)


# ---------------------------------------------------------------------------
# reprocess – verify_doc_pattern
# ---------------------------------------------------------------------------

class ReprocessVerifyTests(unittest.TestCase):
    def test_verify_called_after_step_6_3(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            job = _make_job_dir(Path(d))
            mock_verify = MagicMock(return_value={"found": True, "count": 5, "doc_chars": 1000, "pattern": "[補足:"})

            def fake_run_step(cmd, step, *, dry_run=False):
                return {"full_write_verified": "True"}

            with patch("reprocess_job.run_step", side_effect=fake_run_step), \
                 patch("reprocess_job.backup_job_dir", return_value=job / "_backup"), \
                 patch("reprocess_job.verify_doc_pattern", mock_verify):
                result = reprocess(
                    job,
                    input_root=str(Path(d)),
                    from_step="6.3",
                    input_override=None,
                    verify_pattern="[補足:",
                    credentials="credentials.json",
                    token="token.json",
                    docs_chunk_size=5000,
                    reason="test",
                    dry_run=False,
                )
            mock_verify.assert_called_once_with("DOC_ID_TEST", "[補足:", "credentials.json", "token.json")
            self.assertTrue(result["verify"]["found"])

    def test_verify_skipped_when_pattern_empty(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            job = _make_job_dir(Path(d))
            mock_verify = MagicMock()

            def fake_run_step(cmd, step, *, dry_run=False):
                return {"full_write_verified": "True"}

            with patch("reprocess_job.run_step", side_effect=fake_run_step), \
                 patch("reprocess_job.backup_job_dir", return_value=job / "_backup"), \
                 patch("reprocess_job.verify_doc_pattern", mock_verify):
                reprocess(
                    job,
                    input_root=str(Path(d)),
                    from_step="6.3",
                    input_override=None,
                    verify_pattern="",  # 空 → スキップ
                    credentials="credentials.json",
                    token="token.json",
                    docs_chunk_size=5000,
                    reason="test",
                    dry_run=False,
                )
            mock_verify.assert_not_called()

    def test_verify_error_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            job = _make_job_dir(Path(d))

            def fake_run_step(cmd, step, *, dry_run=False):
                return {"full_write_verified": "True"}

            with patch("reprocess_job.run_step", side_effect=fake_run_step), \
                 patch("reprocess_job.backup_job_dir", return_value=job / "_backup"), \
                 patch("reprocess_job.verify_doc_pattern", side_effect=RuntimeError("auth error")):
                result = reprocess(
                    job,
                    input_root=str(Path(d)),
                    from_step="6.3",
                    input_override=None,
                    verify_pattern="[補足:",
                    credentials="credentials.json",
                    token="token.json",
                    docs_chunk_size=5000,
                    reason="test",
                    dry_run=False,
                )
            self.assertIsNone(result["verify"]["found"])
            self.assertIn("error", result["verify"])


# ---------------------------------------------------------------------------
# reprocess – log writing
# ---------------------------------------------------------------------------

class ReprocessLogTests(unittest.TestCase):
    def test_writes_log_entry(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            job = _make_job_dir(Path(d))

            def fake_run_step(cmd, step, *, dry_run=False):
                return {"full_write_verified": "True"}

            with patch("reprocess_job.run_step", side_effect=fake_run_step), \
                 patch("reprocess_job.backup_job_dir", return_value=job / "_backup"), \
                 patch("reprocess_job.verify_doc_pattern", return_value={"found": True, "count": 5, "doc_chars": 100, "pattern": "[補足:"}):
                reprocess(
                    job,
                    input_root=str(Path(d)),
                    from_step="6.3",
                    input_override=None,
                    verify_pattern="[補足:",
                    credentials="credentials.json",
                    token="token.json",
                    docs_chunk_size=5000,
                    reason="annotation test",
                    dry_run=False,
                )

            log_path = job / "_reprocess_log.jsonl"
            self.assertTrue(log_path.exists())
            entry = json.loads(log_path.read_text(encoding="utf-8").strip())
            self.assertEqual(entry["from_step"], "6.3")
            self.assertEqual(entry["reason"], "annotation test")
            self.assertIn("6.3", entry["steps_run"])
            self.assertFalse(entry["dry_run"])

    def test_dry_run_does_not_write_log(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            job = _make_job_dir(Path(d))
            with patch("reprocess_job.run_step"), \
                 patch("reprocess_job.backup_job_dir", return_value=job / "_backup"):
                reprocess(
                    job,
                    input_root=str(Path(d)),
                    from_step="6.3",
                    input_override=None,
                    verify_pattern="",
                    credentials="credentials.json",
                    token="token.json",
                    docs_chunk_size=5000,
                    reason="dry",
                    dry_run=True,
                )
            self.assertFalse((job / "_reprocess_log.jsonl").exists())


# ---------------------------------------------------------------------------
# step_order constant
# ---------------------------------------------------------------------------

class StepOrderTests(unittest.TestCase):
    def test_6_1_before_6_2(self) -> None:
        self.assertLess(_STEP_ORDER["6.1"], _STEP_ORDER["6.2"])

    def test_6_2_before_6_3(self) -> None:
        self.assertLess(_STEP_ORDER["6.2"], _STEP_ORDER["6.3"])


if __name__ == "__main__":
    unittest.main()
