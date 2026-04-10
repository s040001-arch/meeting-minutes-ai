"""
既存の Google Docs 議事録からナレッジを一括抽出し、Knowledge Sheet に追加する。

使い方:
  python extract_knowledge_from_docs.py --folder-id <FOLDER_ID>

オプション:
  --dry-run      抽出結果を表示するだけで Sheet には書き込まない
  --max-docs N   処理する最大ドキュメント数（デフォルト: 全件）
  --recursive    サブフォルダも再帰的に探索する

必要な環境変数:
  ANTHROPIC_API_KEY        — Claude API キー
  KNOWLEDGE_SHEET_ID       — Knowledge Sheet のスプレッドシート ID（--dry-run 時は不要）
  GOOGLE_SERVICE_ACCOUNT_JSON — サービスアカウント JSON パス（デフォルト: credentials_service_account.json）
"""
import argparse
import json
import os
import sys
import time

import anthropic
import httpx
from google.oauth2 import service_account
from googleapiclient.discovery import build

from knowledge_sheet_store import (
    load_knowledge_memos,
    save_knowledge_memos,
    knowledge_store_enabled,
    _normalize_knowledge_memos,
)

_SA_JSON_PATH_DEFAULT = "credentials_service_account.json"
_DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
]
_EXTRACT_MODEL = "claude-sonnet-4-20250514"
_MERGE_MODEL = "claude-sonnet-4-20250514"
_DOCS_MIME = "application/vnd.google-apps.document"
_FOLDER_MIME = "application/vnd.google-apps.folder"


def _sa_json_path() -> str:
    return os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", _SA_JSON_PATH_DEFAULT).strip()


def _build_credentials() -> service_account.Credentials:
    path = _sa_json_path()
    if not os.path.isfile(path):
        raise FileNotFoundError(f"service account json not found: {path}")
    return service_account.Credentials.from_service_account_file(path, scopes=_DRIVE_SCOPES)


def _build_drive_service():
    return build("drive", "v3", credentials=_build_credentials())


def _build_docs_service():
    return build("docs", "v1", credentials=_build_credentials())


def list_docs_in_folder(
    drive_service,
    folder_id: str,
    *,
    recursive: bool = False,
) -> list[dict]:
    """フォルダ内の Google Docs を一覧取得する。"""
    docs: list[dict] = []
    q = f"'{folder_id}' in parents and trashed=false"
    page_token = None

    while True:
        resp = drive_service.files().list(
            q=q,
            spaces="drive",
            fields="nextPageToken,files(id,name,mimeType,createdTime)",
            pageSize=100,
            pageToken=page_token,
        ).execute()

        for f in resp.get("files", []):
            if f.get("mimeType") == _DOCS_MIME:
                docs.append(f)
            elif recursive and f.get("mimeType") == _FOLDER_MIME:
                sub_docs = list_docs_in_folder(
                    drive_service, f["id"], recursive=True,
                )
                docs.extend(sub_docs)

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    docs.sort(key=lambda x: x.get("createdTime", ""))
    return docs


def fetch_doc_text(docs_service, doc_id: str) -> str:
    """Google Doc の本文テキストを取得する。"""
    doc = docs_service.documents().get(documentId=doc_id).execute()
    body = doc.get("body", {})
    content = body.get("content", [])
    parts: list[str] = []
    for item in content:
        p = item.get("paragraph")
        if not p:
            continue
        for e in p.get("elements", []):
            tr = e.get("textRun")
            if tr and "content" in tr:
                parts.append(tr["content"])
    return "".join(parts)


def _build_extract_prompt() -> str:
    return (
        "あなたは議事録からナレッジを抽出する専門アシスタントです。\n"
        "入力として会議の議事録テキストが与えられます。\n"
        "この議事録から、今後の別の会議の音声認識補正に役立つ情報を抽出してください。\n"
        "\n【抽出対象】\n"
        "1. 人名とその所属・役割\n"
        "   例: 「川口氏はNRE物流事業部の事業企画部所属」\n"
        "2. 会社名・組織名\n"
        "   例: 「プレセナ＝研修サービス提供会社」\n"
        "3. 業界用語・専門用語\n"
        "   例: 「リーシング＝不動産の賃貸借仲介業務」\n"
        "4. 社内用語・造語・略称\n"
        "   例: 「ホワイハウ＝What/Where/Why/How の社内略称」\n"
        "5. 音声認識で誤変換されやすいパターン\n"
        "   例: 「『謁見』は音声認識の誤変換。文脈から『ある件』の可能性」\n"
        "6. 組織の関係性・構造\n"
        "   例: 「NRE物流事業部には営業スキルアップチームが存在」\n"
        "7. プロジェクト名・イベント名\n"
        "   例: 「営業力強化プロジェクト＝2024年から1年間実施」\n"
        "\n【出力形式】\n"
        "JSON配列のみを出力してください。各要素は1件のナレッジ（文字列）です。\n"
        "説明文やコードフェンスは付けないでください。\n"
        "1件のナレッジは1行の簡潔な日本語で記述してください。\n"
        "\n【注意事項】\n"
        "- 会議固有の一時的な情報（日程調整の結果、個別の見積金額等）は含めない\n"
        "- ジョブを跨いで再利用価値のある情報のみを抽出する\n"
        "- 推測や創作は禁止。議事録に明記されている情報のみ抽出する\n"
        "- 同じ情報を重複して出力しない\n"
    )


def extract_knowledge_from_text(
    text: str,
    *,
    doc_name: str = "",
    model: str = _EXTRACT_MODEL,
) -> list[str]:
    """テキストから Claude でナレッジ候補を抽出する。"""
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")

    client = anthropic.Anthropic(
        api_key=api_key,
        timeout=httpx.Timeout(timeout=300.0, connect=30.0),
    )

    user_msg = text.strip()
    if doc_name:
        user_msg = f"【ドキュメント名】{doc_name}\n\n{user_msg}"

    if len(user_msg) > 150_000:
        user_msg = user_msg[:150_000] + "\n\n（以下省略）"

    resp = client.messages.create(
        model=model,
        max_tokens=4000,
        temperature=0,
        system=_build_extract_prompt(),
        messages=[{"role": "user", "content": user_msg}],
    )

    raw = ""
    for block in resp.content:
        if getattr(block, "type", "") == "text":
            raw += block.text

    raw = raw.strip()
    if raw.startswith("```"):
        import re
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        print(f"  WARNING: JSON parse failed for {doc_name}, skipping", file=sys.stderr)
        return []

    if not isinstance(items, list):
        return []

    return [str(item).strip() for item in items if str(item).strip()]


def _build_merge_prompt() -> str:
    return (
        "あなたは議事録AIのナレッジ管理アシスタントです。\n"
        "入力として、既存のナレッジ一覧と、新しく抽出されたナレッジ候補一覧が与えられます。\n"
        "\n【タスク】\n"
        "1. 既存ナレッジと新規ナレッジをマージする\n"
        "2. 重複・類似する項目は統合して1件にまとめる\n"
        "3. 矛盾する情報がある場合は両方残す（判断は人間に委ねる）\n"
        "4. 全体を簡潔で再利用しやすい表現に整える\n"
        "\n【出力形式】\n"
        "JSON配列のみを出力してください。説明文やコードフェンスは付けないでください。\n"
    )


def merge_knowledge_lists(
    existing: list[str],
    new_items: list[str],
    *,
    model: str = _MERGE_MODEL,
) -> list[str]:
    """既存ナレッジと新規抽出をマージする。"""
    if not new_items:
        return list(existing)

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")

    client = anthropic.Anthropic(
        api_key=api_key,
        timeout=httpx.Timeout(timeout=120.0, connect=30.0),
    )

    payload = json.dumps(
        {"existing_knowledge": existing, "new_candidates": new_items},
        ensure_ascii=False,
    )

    resp = client.messages.create(
        model=model,
        max_tokens=4000,
        temperature=0,
        system=_build_merge_prompt(),
        messages=[{"role": "user", "content": payload}],
    )

    raw = ""
    for block in resp.content:
        if getattr(block, "type", "") == "text":
            raw += block.text

    raw = raw.strip()
    if raw.startswith("```"):
        import re
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        print("WARNING: merge JSON parse failed, returning concatenated list", file=sys.stderr)
        return _normalize_knowledge_memos(existing + new_items)

    if not isinstance(items, list):
        return _normalize_knowledge_memos(existing + new_items)

    merged = [str(item).strip() for item in items if str(item).strip()]
    return _normalize_knowledge_memos(merged)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="既存 Google Docs 議事録からナレッジを一括抽出し Knowledge Sheet に追加する"
    )
    parser.add_argument("--folder-id", required=True, help="Google Drive フォルダ ID")
    parser.add_argument("--dry-run", action="store_true", help="抽出結果を表示するだけで Sheet に書き込まない")
    parser.add_argument("--max-docs", type=int, default=0, help="処理する最大ドキュメント数（0=全件）")
    parser.add_argument("--recursive", action="store_true", help="サブフォルダも再帰的に探索する")
    parser.add_argument("--output-json", default=None, help="抽出結果を JSON ファイルに保存する")
    args = parser.parse_args()

    print(f"folder_id={args.folder_id}")
    print(f"dry_run={args.dry_run}")
    print(f"recursive={args.recursive}")

    drive_service = _build_drive_service()
    docs_service = _build_docs_service()

    print("Listing documents in folder...", flush=True)
    all_docs = list_docs_in_folder(drive_service, args.folder_id, recursive=args.recursive)
    print(f"Found {len(all_docs)} Google Docs")

    if args.max_docs > 0:
        all_docs = all_docs[: args.max_docs]
        print(f"Processing first {len(all_docs)} docs (--max-docs)")

    all_extracted: list[str] = []

    for i, doc_meta in enumerate(all_docs, 1):
        doc_id = doc_meta["id"]
        doc_name = doc_meta.get("name", doc_id)
        print(f"\n[{i}/{len(all_docs)}] {doc_name}", flush=True)

        try:
            text = fetch_doc_text(docs_service, doc_id)
        except Exception as e:
            print(f"  ERROR reading doc: {e}", file=sys.stderr)
            continue

        if not text.strip():
            print("  SKIP: empty document")
            continue

        char_count = len(text)
        print(f"  chars={char_count}", flush=True)

        try:
            items = extract_knowledge_from_text(text, doc_name=doc_name)
        except Exception as e:
            print(f"  ERROR extracting: {e}", file=sys.stderr)
            continue

        print(f"  extracted={len(items)} entries")
        for item in items:
            print(f"    - {item}")

        all_extracted.extend(items)

        if i < len(all_docs):
            time.sleep(1.0)

    all_extracted = _normalize_knowledge_memos(all_extracted)
    print(f"\n{'='*60}")
    print(f"Total extracted: {len(all_extracted)} unique entries")

    if args.output_json:
        os.makedirs(os.path.dirname(args.output_json) or ".", exist_ok=True)
        with open(args.output_json, "w", encoding="utf-8") as f:
            json.dump(all_extracted, f, ensure_ascii=False, indent=2)
        print(f"Saved to: {args.output_json}")

    if args.dry_run:
        print("\n[DRY RUN] Extracted knowledge entries:")
        for item in all_extracted:
            print(f"  - {item}")
        print(f"\n[DRY RUN] Would merge with Knowledge Sheet. No changes written.")
        return

    if not knowledge_store_enabled():
        print("\nWARNING: KNOWLEDGE_SHEET_ID not set. Cannot write to sheet.", file=sys.stderr)
        print("Use --output-json to save results to file instead.")
        return

    print("\nLoading existing knowledge from sheet...", flush=True)
    existing = load_knowledge_memos()
    print(f"Existing entries: {len(existing)}")

    print("Merging with Claude...", flush=True)
    merged = merge_knowledge_lists(existing, all_extracted)
    print(f"Merged entries: {len(merged)}")

    if merged == existing:
        print("No changes needed. Knowledge sheet is up to date.")
        return

    print("Saving to Knowledge Sheet...", flush=True)
    save_knowledge_memos(merged)
    print(f"Done! Updated: {len(existing)} → {len(merged)} entries")


if __name__ == "__main__":
    main()
