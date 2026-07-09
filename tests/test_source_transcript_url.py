"""Tests for source transcript URL resolution."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from transcript_paths import drive_file_view_url, load_source_transcript_url


class SourceTranscriptUrlTests(unittest.TestCase):
    def test_drive_file_view_url(self) -> None:
        self.assertEqual(
            drive_file_view_url("abc123"),
            "https://drive.google.com/file/d/abc123/view",
        )
        self.assertEqual(drive_file_view_url(""), "")

    def test_load_from_hub_source_file_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            (job / "google_doc_hub.json").write_text(
                json.dumps({"source_file_url": "https://drive.example/original.txt"}),
                encoding="utf-8",
            )
            self.assertEqual(
                load_source_transcript_url(str(job)),
                "https://drive.example/original.txt",
            )

    def test_load_from_hub_source_drive_file_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            (job / "google_doc_hub.json").write_text(
                json.dumps({"source_drive_file_id": "file999"}),
                encoding="utf-8",
            )
            self.assertEqual(
                load_source_transcript_url(str(job)),
                "https://drive.google.com/file/d/file999/view",
            )

    def test_load_from_docs_write_log_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            (job / "docs_write_log.txt").write_text(
                "job_id=test\nuploaded_drive_file_id=logfile42\n",
                encoding="utf-8",
            )
            self.assertEqual(
                load_source_transcript_url(str(job)),
                "https://drive.google.com/file/d/logfile42/view",
            )


if __name__ == "__main__":
    unittest.main()
