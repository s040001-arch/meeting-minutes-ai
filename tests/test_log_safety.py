"""Tests for log_safety sanitization."""
from __future__ import annotations

import unittest

from log_safety import (
    describe_service_account_path,
    format_error_for_log,
    sanitize_log_text,
)


class LogSafetyTests(unittest.TestCase):
    def test_inline_json_path_not_leaked(self) -> None:
        inline = '{"type":"service_account","private_key":"SECRET"}'
        desc = describe_service_account_path(inline)
        self.assertNotIn("SECRET", desc)
        self.assertIn("inline_json", desc)

    def test_file_not_found_error_sanitized(self) -> None:
        inline = '{"type":"service_account","private_key":"-----BEGIN PRIVATE KEY-----\\nABC\\n-----END PRIVATE KEY-----\\n"}'
        err = FileNotFoundError(f"service account json not found: {describe_service_account_path(inline)}")
        logged = format_error_for_log(err)
        self.assertNotIn("BEGIN PRIVATE KEY", logged)
        self.assertNotIn("ABC", logged)
        self.assertIn("FileNotFoundError", logged)

    def test_sanitize_private_key_block(self) -> None:
        raw = 'failed: {"private_key": "-----BEGIN PRIVATE KEY-----\\nXYZ\\n-----END PRIVATE KEY-----\\n"}'
        out = sanitize_log_text(raw)
        self.assertNotIn("BEGIN PRIVATE KEY", out)
        self.assertIn("REDACTED", out)


if __name__ == "__main__":
    unittest.main()
