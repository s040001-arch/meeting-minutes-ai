import argparse
import json
import os
import re
from typing import List, Tuple

from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from googleapiclient.errors import HttpError


# Google Docs作成 + Drive上で配置変更するためのスコープ
DOCS_SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]


def md_to_google_docs_text(md: str) -> str:
    """
    MarkdownをGoogle Docsへ貼り付けやすいプレーンテキストに最小変換する。
    見出し記号（#）は除去してプレーンテキストにする。
    見出しスタイルの適用は apply_heading_styles() で別途行う。
    """
    lines: List[str] = []
    for raw in md.splitlines():
        line = raw.strip()
        if not line:
            lines.append("")
            continue

        # 見出し（# を除去してプレーンテキスト化）
        line = re.sub(r"^#{1,6}\s*", "", line)

        # 箇条書き
        if line.startswith("- "):
            line = f"* {line[2:]}"

        lines.append(line)
    return "\n".join(lines).strip() + "\n"


def _parse_heading_map(md: str) -> dict:
    """Markdown の # / ## 行から {プレーンテキスト: namedStyleType} の辞書を生成する。

    # タイトル  → "TITLE"
    ## セクション → "HEADING_2"
    """
    heading_map: dict = {}
    for line in md.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            heading_map[stripped[3:].strip()] = "HEADING_2"
        elif stripped.startswith("# "):
            heading_map[stripped[2:].strip()] = "TITLE"
    return heading_map


def apply_heading_styles(docs_service, doc_id: str, heading_map: dict) -> None:
    """挿入済みテキストの段落に Heading / Title スタイルを適用する。

    Google Docs 上で段落テキストが heading_map のキーと一致した場合、
    対応する namedStyleType（TITLE または HEADING_2）を updateParagraphStyle で設定する。
    """
    if not heading_map:
        return
    doc = docs_service.documents().get(documentId=doc_id).execute()
    body = doc.get("body", {})
    content = body.get("content", [])

    style_requests: List[dict] = []
    for item in content:
        paragraph = item.get("paragraph")
        if not paragraph:
            continue
        start_idx = item.get("startIndex")
        end_idx = item.get("endIndex")
        if start_idx is None or end_idx is None:
            continue
        para_text = "".join(
            el.get("textRun", {}).get("content", "")
            for el in paragraph.get("elements", [])
        ).strip()
        if para_text in heading_map:
            style_requests.append({
                "updateParagraphStyle": {
                    "range": {"startIndex": start_idx, "endIndex": end_idx},
                    "paragraphStyle": {"namedStyleType": heading_map[para_text]},
                    "fields": "namedStyleType",
                }
            })

    # 50件ずつ送信（API リクエスト制限対策）
    for i in range(0, len(style_requests), 50):
        docs_service.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": style_requests[i : i + 50]},
        ).execute()


def resolve_input(minutes_structured_path: str | None, job_id: str, input_root: str) -> str:
    if minutes_structured_path:
        return minutes_structured_path
    return os.path.join(input_root, job_id, "minutes_structured.md")


def resolve_output_dir(job_id: str, output_root: str) -> str:
    out_dir = os.path.join(output_root, job_id)
    os.makedirs(out_dir, exist_ok=True)
    return out_dir


def load_or_create_google_docs_credentials(
    credentials_json_path: str,
    token_json_path: str,
) -> Credentials:
    creds: Credentials | None = None
    if os.path.exists(token_json_path):
        creds = Credentials.from_authorized_user_file(token_json_path, DOCS_SCOPES)

    if creds and creds.valid:
        return creds

    # token が期限切れ/無効の場合、まず refresh を試す。
    # ただし `invalid_scope` のようなスコープ不整合だと refresh できないため、
    # その場合は再認可（外部UIが必要になり得る）へフォールバックする。
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            return creds
        except RefreshError:
            # scope mismatch などで refresh ができない場合は作り直す
            creds = None

    # token がない or refresh 不能の場合、Railway ではブラウザ認証が使えないためエラーで終了する。
    # ローカルで token.json を再生成し GOOGLE_OAUTH_TOKEN_JSON 環境変数を更新すること。
    raise RuntimeError(
        "token.json のリフレッシュに失敗しました。"
        " Railway ではブラウザ認証ができないため、ローカルで token.json を再生成し、"
        " GOOGLE_OAUTH_TOKEN_JSON 環境変数を更新してください。"
    )


def create_google_doc_and_insert_text(
    docs_service,
    title: str,
    text: str,
    chunk_size: int,
) -> Tuple[str, List[dict]]:
    chunks = split_text_into_chunks(text, max_chars=chunk_size)
    doc = docs_service.documents().create(body={"title": title}).execute()
    doc_id = doc.get("documentId")
    if not doc_id:
        raise RuntimeError("failed to create google doc: missing documentId")

    # 長文の欠損を避けるため、チャンク単位で末尾に積み上げる
    insert_index = 1
    inserted_total = 0
    insert_logs: List[dict] = []
    for i, chunk in enumerate(chunks, start=1):
        req = [{"insertText": {"location": {"index": insert_index}, "text": chunk}}]
        docs_service.documents().batchUpdate(documentId=doc_id, body={"requests": req}).execute()
        chunk_len = len(chunk)
        inserted_total += chunk_len
        insert_index += chunk_len
        log = {
            "chunk_index": i,
            "chunk_count": len(chunks),
            "chunk_chars": chunk_len,
            "inserted_total_chars": inserted_total,
        }
        insert_logs.append(log)
        print(
            f"insert_chunk={i}/{len(chunks)} chunk_chars={chunk_len} inserted_total={inserted_total}"
        )

    return str(doc_id), insert_logs


def get_document_body_end_index(doc: dict) -> int:
    """本文の終端インデックス（deleteContentRange の end に使う値の参考）。"""
    body = doc.get("body", {})
    content = body.get("content", [])
    if not content:
        return 2
    return int(content[-1].get("endIndex", 2))


def clear_document_body(docs_service, doc_id: str) -> None:
    """既存 Docs の本文を空にする（タイトルは Drive 上のファイル名として残る）。"""
    doc = docs_service.documents().get(documentId=doc_id).execute()
    end_index = get_document_body_end_index(doc)
    # Google サンプル: 末尾の改行を除き [1, endIndex-1) を削除
    if end_index <= 2:
        return
    req = [
        {
            "deleteContentRange": {
                "range": {
                    "startIndex": 1,
                    "endIndex": end_index - 1,
                }
            }
        }
    ]
    docs_service.documents().batchUpdate(documentId=doc_id, body={"requests": req}).execute()


def insert_text_at_start(
    docs_service,
    doc_id: str,
    text: str,
    chunk_size: int,
) -> List[dict]:
    """本文先頭（index 1）から順に挿入（長文はチャンク分割）。"""
    chunks = split_text_into_chunks(text, max_chars=chunk_size)
    insert_index = 1
    inserted_total = 0
    insert_logs: List[dict] = []
    for i, chunk in enumerate(chunks, start=1):
        req = [{"insertText": {"location": {"index": insert_index}, "text": chunk}}]
        docs_service.documents().batchUpdate(documentId=doc_id, body={"requests": req}).execute()
        chunk_len = len(chunk)
        inserted_total += chunk_len
        insert_index += chunk_len
        insert_logs.append(
            {
                "chunk_index": i,
                "chunk_count": len(chunks),
                "chunk_chars": chunk_len,
                "inserted_total_chars": inserted_total,
            }
        )
        print(
            f"insert_chunk={i}/{len(chunks)} chunk_chars={chunk_len} inserted_total={inserted_total}"
        )
    return insert_logs


def replace_google_doc_body(
    docs_service,
    doc_id: str,
    text: str,
    chunk_size: int,
) -> List[dict]:
    clear_document_body(docs_service, doc_id)
    return insert_text_at_start(docs_service, doc_id, text, chunk_size)


def split_text_into_chunks(text: str, max_chars: int) -> List[str]:
    if max_chars <= 0:
        raise ValueError("max_chars must be > 0")
    if not text:
        return [""]

    chunks: List[str] = []
    current: List[str] = []
    current_len = 0
    for line in text.splitlines(keepends=True):
        line_len = len(line)
        if current and current_len + line_len > max_chars:
            chunks.append("".join(current))
            current = []
            current_len = 0

        if line_len > max_chars:
            # 1行が上限を超える場合は強制分割
            start = 0
            while start < line_len:
                remain = max_chars - current_len
                if remain == 0:
                    chunks.append("".join(current))
                    current = []
                    current_len = 0
                    remain = max_chars
                part = line[start : start + remain]
                current.append(part)
                current_len += len(part)
                start += len(part)
                if current_len >= max_chars:
                    chunks.append("".join(current))
                    current = []
                    current_len = 0
        else:
            current.append(line)
            current_len += line_len

    if current:
        chunks.append("".join(current))
    return chunks


def fetch_google_doc_text(docs_service, doc_id: str) -> str:
    doc = docs_service.documents().get(documentId=doc_id).execute()
    body = doc.get("body", {})
    content = body.get("content", [])
    parts: List[str] = []
    for item in content:
        p = item.get("paragraph")
        if not p:
            continue
        for e in p.get("elements", []):
            tr = e.get("textRun")
            if tr and "content" in tr:
                parts.append(tr["content"])
    return "".join(parts)


def ensure_drive_subfolder(drive_service, parent_folder_id: str, subfolder_name: str) -> str:
    safe_name = subfolder_name.strip()
    if not safe_name:
        raise ValueError("subfolder name is empty.")

    # 同名フォルダが既にあれば再利用
    escaped = safe_name.replace("\\", "\\\\").replace("'", "\\'")
    query = (
        f"'{parent_folder_id}' in parents and "
        "mimeType='application/vnd.google-apps.folder' and "
        f"name='{escaped}' and trashed=false"
    )
    result = (
        drive_service.files()
        .list(q=query, spaces="drive", fields="files(id,name)", pageSize=10)
        .execute()
    )
    files = result.get("files", [])
    if files:
        return str(files[0]["id"])

    created = (
        drive_service.files()
        .create(
            body={
                "name": safe_name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_folder_id],
            },
            fields="id,name",
        )
        .execute()
    )
    return str(created["id"])


def upload_local_file_to_drive_folder(
    drive_service,
    local_path: str,
    folder_id: str,
) -> str:
    """
    ローカルファイルを指定 Drive フォルダにアップロードする。
    戻り値は作成されたファイルの id。
    """
    if not os.path.isfile(local_path):
        raise FileNotFoundError(f"upload source not found: {local_path}")
    name = os.path.basename(local_path)
    lower = name.lower()
    if lower.endswith(".txt"):
        mime = "text/plain"
    elif lower.endswith(".md"):
        mime = "text/markdown"
    elif lower.endswith(".m4a"):
        mime = "audio/mp4"
    elif lower.endswith(".mp3"):
        mime = "audio/mpeg"
    elif lower.endswith(".wav"):
        mime = "audio/wav"
    else:
        mime = "application/octet-stream"
    meta = {"name": name, "parents": [folder_id]}
    media = MediaFileUpload(local_path, mimetype=mime, resumable=True)
    created = (
        drive_service.files()
        .create(body=meta, media_body=media, fields="id,name", supportsAllDrives=True)
        .execute()
    )
    fid = created.get("id")
    if not fid:
        raise RuntimeError("Drive upload failed: missing file id in response")
    return str(fid)


def move_file_to_folder(drive_service, file_id: str, folder_id: str) -> None:
    file_meta = drive_service.files().get(fileId=file_id, fields="id,parents").execute()
    prev_parents = ",".join(file_meta.get("parents", []))
    drive_service.files().update(
        fileId=file_id,
        addParents=folder_id,
        removeParents=prev_parents,
        fields="id, parents",
    ).execute()


def main() -> None:
    parser = argparse.ArgumentParser(description="Task 6-3: 発言録をGoogle Docsへ出力")
    parser.add_argument("--job-id", required=True, help="対象ジョブID")
    parser.add_argument(
        "--input-root",
        default="data/transcriptions",
        help="ジョブディレクトリのルート（デフォルト: data/transcriptions）",
    )
    parser.add_argument(
        "--input",
        default=None,
        help="minutes_structured.md のパス（未指定時: {input_root}/{job_id}/minutes_structured.md）",
    )
    parser.add_argument(
        "--output-root",
        default="data/google_docs_export",
        help="dry-run出力ルート（デフォルト: data/google_docs_export）",
    )
    parser.add_argument(
        "--title",
        default=None,
        help="Docsファイル名（未指定時: job_id）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Google Docsへ書き込まず、ローカルへ出力（デフォルト挙動: dry-run）",
    )
    parser.set_defaults(dry_run=True)

    # push時（外部API/認可が必要になり得る）
    parser.add_argument(
        "--push",
        action="store_true",
        help="dry-runを無効にし、Google Docs APIへ書き込む",
    )
    parser.add_argument(
        "--credentials",
        default="credentials.json",
        help="Google OAuthクライアントJSON（デフォルト: credentials.json）",
    )
    parser.add_argument(
        "--token",
        default="token.json",
        help="OAuthトークン保存先/読み込み先（デフォルト: token.json）",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="（互換用）未使用。将来拡張用のため残す",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=5000,
        help="Google Docsへ分割挿入する際の1チャンク文字数上限（デフォルト: 5000）",
    )
    parser.add_argument(
        "--drive-parent-folder-id",
        default=None,
        help="指定時: 作成したDocsをこのDriveフォルダ配下へ配置する",
    )
    parser.add_argument(
        "--drive-subfolder-name",
        default=None,
        help="指定時: 親フォルダ配下にこの名前のサブフォルダを作成/再利用してDocsを入れる",
    )
    parser.add_argument(
        "--update-doc-id",
        default=None,
        help="指定時: 新規作成せず、この documentId の本文を差し替える（--push 必須）",
    )
    parser.add_argument(
        "--write-doc-meta-json",
        default=None,
        help="指定時: doc_id と URL をこのパスへ JSON 書き出し（同一 job の再更新用）",
    )
    parser.add_argument(
        "--upload-local-file",
        default=None,
        help=(
            "指定時: Docs を配置したのと同じ Drive サブフォルダへ、このローカルファイルをアップロードする "
            "（--drive-parent-folder-id でサブフォルダが決まった場合のみ有効）"
        ),
    )
    parser.add_argument(
        "--delete-local-after-upload",
        action="store_true",
        help="--upload-local-file 成功後にローカル元ファイルを削除する（移動に近い挙動）",
    )
    args = parser.parse_args()

    if args.update_doc_id and not args.push:
        raise ValueError("--update-doc-id requires --push (replace body is only meaningful when uploading)")

    in_path = resolve_input(args.input, args.job_id, args.input_root)
    if not os.path.isfile(in_path):
        raise FileNotFoundError(f"input file not found: {in_path}")

    with open(in_path, "r", encoding="utf-8") as f:
        md = f.read()

    text = md_to_google_docs_text(md)

    title = args.title or args.job_id
    out_dir = resolve_output_dir(args.job_id, args.output_root)
    out_text_path = os.path.join(out_dir, "docs_text.txt")
    out_payload_path = os.path.join(out_dir, "docs_payload_preview.txt")
    with open(out_text_path, "w", encoding="utf-8") as f:
        f.write(text)
    with open(out_payload_path, "w", encoding="utf-8") as f:
        f.write(f"title={title}\n")
        f.write(f"input={in_path}\n")
        f.write(f"chars={len(text)}\n")
        f.write(f"chunk_size={args.chunk_size}\n")

    # dry-run のみで終了（外部UI不要）
    if not args.push:
        print(f"job_id={args.job_id}")
        print(f"dry_run=1")
        print(f"title={title}")
        print(f"saved_text={out_text_path}")
        return

    # push モード：外部APIアクセス＆OAuth認可が必要になり得る
    creds = load_or_create_google_docs_credentials(
        credentials_json_path=args.credentials,
        token_json_path=args.token,
    )
    docs_service = build("docs", "v1", credentials=creds)
    drive_service = build("drive", "v3", credentials=creds)

    if args.update_doc_id:
        doc_id = args.update_doc_id.strip()
        insert_logs = replace_google_doc_body(
            docs_service=docs_service,
            doc_id=doc_id,
            text=text,
            chunk_size=args.chunk_size,
        )
    else:
        doc_id, insert_logs = create_google_doc_and_insert_text(
            docs_service=docs_service,
            title=title,
            text=text,
            chunk_size=args.chunk_size,
        )

    # 見出しスタイルを適用（Title / Heading 2）
    # 失敗してもテキスト挿入済みなので続行する（スタイルなしで Docs に残る）
    heading_map = _parse_heading_map(md)
    if heading_map:
        try:
            apply_heading_styles(docs_service, doc_id, heading_map)
        except Exception as _heading_err:
            print(f"apply_heading_styles_failed={_heading_err!r} (non-fatal, continuing)")

    actual_text = fetch_google_doc_text(docs_service, doc_id)
    expected_chars = len(text)
    actual_chars = len(actual_text)
    same_content_raw = actual_text == text
    normalized_expected = text.rstrip("\n")
    normalized_actual = actual_text.rstrip("\n")
    same_content_normalized = normalized_actual == normalized_expected
    char_delta = actual_chars - expected_chars

    doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"

    # 同一ドキュメントを更新するとき、Drive 上のファイル名も --title に合わせる（ステータス接頭辞の付け替え用）
    if args.push and args.title and args.update_doc_id:
        drive_service.files().update(
            fileId=doc_id,
            body={"name": args.title.strip()},
            fields="id,name",
        ).execute()

    write_log_path = os.path.join(out_dir, "docs_write_log.txt")
    with open(write_log_path, "w", encoding="utf-8") as f:
        f.write(f"job_id={args.job_id}\n")
        f.write(f"title={title}\n")
        f.write(f"doc_id={doc_id}\n")
        f.write(f"doc_url={doc_url}\n")
        f.write(f"expected_chars={expected_chars}\n")
        f.write(f"actual_chars={actual_chars}\n")
        f.write(f"exact_match_raw={same_content_raw}\n")
        f.write(f"exact_match_normalized={same_content_normalized}\n")
        f.write(f"char_delta={char_delta}\n")
        for row in insert_logs:
            f.write(
                "chunk={chunk_index}/{chunk_count} chunk_chars={chunk_chars} inserted_total={inserted_total_chars}\n".format(
                    **row
                )
            )

    target_folder_id = None
    # 新規作成時のみ Drive フォルダへ配置（既存 doc 更新では既存の場所を維持）
    if args.drive_parent_folder_id and not args.update_doc_id:
        subfolder_name = args.drive_subfolder_name or title
        target_folder_id = ensure_drive_subfolder(
            drive_service=drive_service,
            parent_folder_id=args.drive_parent_folder_id,
            subfolder_name=subfolder_name,
        )
        move_file_to_folder(
            drive_service=drive_service,
            file_id=doc_id,
            folder_id=target_folder_id,
        )
        with open(write_log_path, "a", encoding="utf-8") as f:
            f.write(f"drive_parent_folder_id={args.drive_parent_folder_id}\n")
            f.write(f"drive_subfolder_name={subfolder_name}\n")
            f.write(f"target_folder_id={target_folder_id}\n")

        if args.upload_local_file:
            up_path = os.path.abspath(args.upload_local_file.strip())
            try:
                up_id = upload_local_file_to_drive_folder(
                    drive_service=drive_service,
                    local_path=up_path,
                    folder_id=target_folder_id,
                )
                print(f"drive_uploaded_local_file_id={up_id} path={up_path}")
                with open(write_log_path, "a", encoding="utf-8") as f:
                    f.write(f"upload_local_file={up_path}\n")
                    f.write(f"uploaded_drive_file_id={up_id}\n")
                if args.delete_local_after_upload:
                    os.remove(up_path)
                    print(f"deleted_local_after_upload={up_path}")
                    with open(write_log_path, "a", encoding="utf-8") as f:
                        f.write(f"deleted_local_after_upload={up_path}\n")
            except OSError as e:
                with open(write_log_path, "a", encoding="utf-8") as f:
                    f.write(f"upload_local_file_error={e}\n")
                raise

    if args.write_doc_meta_json:
        meta_path = args.write_doc_meta_json
        os.makedirs(os.path.dirname(meta_path) or ".", exist_ok=True)
        existing_meta = {}
        if os.path.isfile(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    existing_meta = loaded
            except Exception:
                existing_meta = {}
        meta_payload = {
            "job_id": args.job_id,
            "doc_id": doc_id,
            "doc_url": doc_url,
            "title": title,
            "mode": "update" if args.update_doc_id else "create",
        }
        folder_id = args.drive_parent_folder_id or existing_meta.get("folder_id")
        subfolder_id = target_folder_id or existing_meta.get("subfolder_id")
        if folder_id:
            meta_payload["folder_id"] = folder_id
        if subfolder_id:
            meta_payload["subfolder_id"] = subfolder_id
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta_payload, f, ensure_ascii=False, indent=2)

    print(f"job_id={args.job_id}")
    print(f"dry_run=0")
    print(f"title={title}")
    print(f"google_doc_id={doc_id}")
    print(f"doc_url={doc_url}")
    print(f"expected_chars={expected_chars}")
    print(f"actual_chars={actual_chars}")
    print(f"exact_match_raw={same_content_raw}")
    print(f"exact_match_normalized={same_content_normalized}")
    print(f"char_delta={char_delta}")
    print(f"full_write_verified={same_content_normalized}")
    if target_folder_id:
        print(f"target_folder_id={target_folder_id}")
    print(f"write_log={write_log_path}")


if __name__ == "__main__":
    main()

