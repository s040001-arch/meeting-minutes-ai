import argparse
import os
from typing import Any, Dict, List, Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow


SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def _load_credentials(
    credentials_json_path: str,
    token_json_path: str,
) -> Credentials:
    """
    OAuth認証を行い、Drive API呼び出しに利用するCredentialsを返す。
    初回はブラウザで認可が必要。
    """
    creds: Optional[Credentials] = None
    if os.path.exists(token_json_path):
        creds = Credentials.from_authorized_user_file(token_json_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                credentials_json_path,
                SCOPES,
            )
            creds = flow.run_local_server(port=0)

        os.makedirs(os.path.dirname(token_json_path), exist_ok=True)
        with open(token_json_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    return creds


def build_drive_service(creds: Credentials):
    # discovery.build は API のバージョン・リソース取得を内部的に行う
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
        "--credentials",
        required=True,
        help="OAuthクライアント設定（credentials.json）のパス",
    )
    parser.add_argument(
        "--token",
        default="token.json",
        help="アクセストークン保存先（未設定ならローカルtoken.json）",
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

    creds = _load_credentials(args.credentials, args.token)
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

