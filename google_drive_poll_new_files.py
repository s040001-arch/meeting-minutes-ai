import argparse
import json
import os
from typing import Any, Dict, List, Optional, Set

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
    """folder_id を親とする直下アイテムのみ（サブフォルダ内のファイルは含まない）。"""
    query = f"'{folder_id}' in parents and trashed = false"
    fields = "nextPageToken, files(id, name, modifiedTime, size, mimeType)"

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


def load_last_seen_ids(path: str) -> Set[str]:
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        return set()
    return {str(x) for x in data}


def save_last_seen_ids(path: str, ids: Set[str]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser(
        description="Google Driveの指定フォルダから新規ファイル検出（Task 1-2）"
    )
    parser.add_argument(
        "--folder-id",
        required=True,
        help="対象Google DriveフォルダID",
    )
    parser.add_argument(
        "--state",
        default="data/last_seen_file_ids.json",
        help="前回取得時のfile_id集合（JSON配列）保存先",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=200,
        help="1ページあたり取得件数（デフォルト200）",
    )
    parser.add_argument(
        "--update-state",
        action="store_true",
        help="差分検出後、stateを現在のfile_id集合で上書きする",
    )
    args = parser.parse_args()

    last_seen_ids = load_last_seen_ids(args.state)

    creds = _build_credentials()
    service = build_drive_service(creds)

    try:
        files = list_files_in_folder(
            service,
            folder_id=args.folder_id,
            page_size=args.page_size,
        )
    except HttpError as e:
        raise RuntimeError(f"Drive API error: {e}") from e

    current_ids = {str(f.get("id")) for f in files if f.get("id")}
    new_ids = current_ids - last_seen_ids
    new_files = [f for f in files if str(f.get("id")) in new_ids]

    print(f"folder_id={args.folder_id}")
    print(f"previous_count={len(last_seen_ids)}")
    print(f"current_count={len(current_ids)}")
    print(f"new_count={len(new_files)}")
    for f in sorted(new_files, key=lambda x: x.get("name") or ""):
        print(f"- {f.get('name')} (id={f.get('id')})")

    # Task 1-2のMVPでは、検出して返した後にstateを更新して重複検出を防ぐ方針。
    if args.update_state:
        save_last_seen_ids(args.state, current_ids)
        print(f"updated_state={args.state}")


if __name__ == "__main__":
    main()

