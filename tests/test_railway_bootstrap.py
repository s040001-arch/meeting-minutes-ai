from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from railway_bootstrap import (
    resolve_google_service_account_path,
    write_google_oauth_files_from_env,
)


class RailwayCredentialBootstrapTests(unittest.TestCase):
    def test_inline_json_resolves_to_materialized_default_file(self) -> None:
        with patch.dict(
            os.environ,
            {"GOOGLE_SERVICE_ACCOUNT_JSON": '{"type":"service_account"}'},
            clear=False,
        ):
            path = resolve_google_service_account_path()
        self.assertTrue(path.endswith("credentials_service_account.json"))
        self.assertFalse(path.startswith("{"))

    def test_path_value_is_not_written_as_json_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            existing = Path(tmp) / "service-account.json"
            existing.write_text("{}", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "GOOGLE_SERVICE_ACCOUNT_JSON": str(existing),
                    "GOOGLE_OAUTH_TOKEN_JSON": "",
                    "GOOGLE_OAUTH_CREDENTIALS_JSON": "",
                },
                clear=False,
            ):
                self.assertEqual(
                    resolve_google_service_account_path(),
                    str(existing),
                )
                written = write_google_oauth_files_from_env()
            self.assertEqual(written, [])


if __name__ == "__main__":
    unittest.main()
