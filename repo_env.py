"""
リポジトリ直下の .env を読み、まだ無い環境変数だけ埋める。
既にシェルや OS で設定されている値は上書きしない。
依存ライブラリは使わない（1ファイルで完結）。
"""

from __future__ import annotations

import os


def _repo_root() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def load_dotenv_local(base_dir: str | None = None) -> bool:
    """
    base_dir/.env があれば読み込む。未指定時は本ファイルと同じディレクトリ（リポジトリルート）。
    成功時 True（ファイルが存在し開けた場合）。
    """
    root = base_dir if base_dir is not None else _repo_root()
    path = os.path.join(root, ".env")
    if not os.path.isfile(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                if not key:
                    continue
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                    val = val[1:-1]
                if key not in os.environ:
                    os.environ[key] = val
    except OSError:
        return False
    return True
