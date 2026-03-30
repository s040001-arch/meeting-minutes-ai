import argparse
import os
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload


SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def _load_credentials(credentials_json_path: str, token_json_path: str) -> Credentials:
    creds: Optional[Credentials] = None
    if os.path.exists(token_json_path):
        creds = Credentials.from_authorized_user_file(token_json_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_json_path, SCOPES)
            creds = flow.run_local_server(port=0)

        os.makedirs(os.path.dirname(token_json_path) or ".", exist_ok=True)
        with open(token_json_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    return creds


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Google Driveのfile_idを指定してローカルへダウンロード（Task 1-3）"
    )
    parser.add_argument("--credentials", required=True, help="credentials.json のパス")
    parser.add_argument("--token", default="token.json", help="token.json の保存先/読み込み先")
    parser.add_argument("--file-id", required=True, help="ダウンロード対象の Google Drive file_id")
    parser.add_argument("--output", required=True, help="保存先ファイルパス（例: data/sample_audio.wav）")
    args = parser.parse_args()

    creds = _load_credentials(args.credentials, args.token)
    service = build("drive", "v3", credentials=creds)

    try:
        request = service.files().get_media(fileId=args.file_id)
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)

        with open(args.output, "wb") as f:
            downloader = MediaIoBaseDownload(f, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()
                if status is not None:
                    print(f"download_progress={int(status.progress() * 100)}%")
    except HttpError as e:
        raise RuntimeError(f"Drive API error: {e}") from e

    print(f"downloaded_file_id={args.file_id}")
    print(f"saved_to={args.output}")


if __name__ == "__main__":
    main()

