"""
Railway 等の PaaS ではリポジトリに credentials をコミットできないため、
環境変数からサービスアカウントファイルを生成する。

設定例（Railway の Variables）:
  GOOGLE_SERVICE_ACCOUNT_JSON   … credentials_service_account.json の全文
                                   （Drive / Docs / Sheets すべてに使用）
"""

from __future__ import annotations

import os


def _repo_root() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def write_google_oauth_files_from_env() -> list[str]:
    """環境変数から OAuth ファイルを生成。作成・スキップしたパスを返す。"""
    root = _repo_root()
    written: list[str] = []

    def write_if_env(path: str, env_name: str) -> None:
        raw = os.getenv(env_name, "").strip()
        if not raw:
            return
        # 破損した token.json が残るケースを防ぐため、常に環境変数の値で上書きする
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(raw)
        written.append(path)

    write_if_env(
        os.path.join(root, "credentials_service_account.json"),
        "GOOGLE_SERVICE_ACCOUNT_JSON",
    )
    return written


if __name__ == "__main__":
    paths = write_google_oauth_files_from_env()
    for p in paths:
        print(f"railway_bootstrap_written={p}")
    if not paths:
        print("railway_bootstrap_written=(none; GOOGLE_SERVICE_ACCOUNT_JSON unset)")
