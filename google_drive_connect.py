import argparse
import os
from typing import Any, Dict, List, Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2 import service_account


SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
_SA_JSON_PATH = "credentials_service_account.json"


def _build_credentials() -> service_account.Credentials:
    """サービスアカウントで Drive 認証情報を生成する。"""
    return service_account.Credentials.from_service_account_file(
        _SA_JSON_PATH, scopes=SCOPES
    )


def build_drive_service(creds: service_account.Credentials):
    return build("drive", "v3", credentials=creds)


def list_files_in_folder(
    service,
    folder_id: str,
    page_size: int = 200,
) -> List[Dict[str, Any]]:
    """
    指定フォルダ直下のファイル一覧を取得する。
    Task 1-1 の成功条件（ファイル名が一覧で取れる）を満たすため、
    必要最小限のフィールドのみ返す。
    """
    query = f"'{folder_id}' in parents and trashed = false"
    fields = "nextPageToken, files(id, name, modifiedTime, size)"

    files: List[Dict[str, Any]] = []
    page_token: Optional[str] = None
    while True:
        request = (
            service.files()
            .list(
                q=query,
                spaces="drive",
                fields=fields,
                pageToken=page_token,
                pageSize=page_size,
            )
        )
        result = request.execute()
        files.extend(result.get("files", []))

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return files


def main():
    parser = argparse.ArgumentParser(
        description="Google Drive接続＆指定フォルダのファイル一覧取得（Task 1-1）"
    )
    parser.add_argument(
        "--folder-id",
        required=True,
        help="対象Google DriveフォルダID",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=200,
        help="1ページあたり取得件数（デフォルト200）",
    )
    args = parser.parse_args()

    creds = _build_credentials()
    service = build_drive_service(creds)

    try:
        files = list_files_in_folder(
            service,
            folder_id=args.folder_id,
            page_size=args.page_size,
        )
    except HttpError as e:
        # Drive APIエラーをそのまま握りつぶさず、原因追跡できる形で出す
        raise RuntimeError(f"Drive API error: {e}") from e

    # 成功条件：ファイル名が一覧として取れること
    print(f"folder_id={args.folder_id}")
    print(f"file_count={len(files)}")
    for f in files:
        print(f"- {f.get('name')} (id={f.get('id')})")


if __name__ == "__main__":
    main()

