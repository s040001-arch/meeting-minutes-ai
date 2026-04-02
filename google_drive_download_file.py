import argparse
import os

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload


SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
_SA_JSON_PATH = "credentials_service_account.json"


def _build_credentials() -> service_account.Credentials:
    """サービスアカウントで Drive 認証情報を生成する。"""
    return service_account.Credentials.from_service_account_file(
        _SA_JSON_PATH, scopes=SCOPES
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Google Driveのfile_idを指定してローカルへダウンロード（Task 1-3）"
    )
    parser.add_argument("--file-id", required=True, help="ダウンロード対象の Google Drive file_id")
    parser.add_argument("--output", required=True, help="保存先ファイルパス（例: data/sample_audio.wav）")
    args = parser.parse_args()

    creds = _build_credentials()
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

