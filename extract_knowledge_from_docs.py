"""
既存の Google Docs 議事録からナレッジを一括抽出し、Knowledge Sheet に追加する。

使い方:
  python extract_knowledge_from_docs.py --folder-id <FOLDER_ID>

オプション:
  --dry-run      抽出結果を表示するだけで Sheet には書き込まない
  --max-docs N   処理する最大ドキュメント数（デフォルト: 全件）
  --recursive    サブフォルダも再帰的に探索する
  --log-file F   ログをファイルにも出力する

必要な環境変数:
  ANTHROPIC_API_KEY        — Claude API キー
  KNOWLEDGE_SHEET_ID       — Knowledge Sheet のスプレッドシート ID（--dry-run 時は不要）
  GOOGLE_SERVICE_ACCOUNT_JSON — サービスアカウント JSON パス（デフォルト: credentials_service_account.json）
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

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

_LOG_FILE = None


def _log(msg: str, *, level: str = "INFO") -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    print(line, flush=True)
    if _LOG_FILE:
        try:
            with open(_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def _log_error(msg: str) -> None:
    _log(msg, level="ERROR")


def _log_warn(msg: str) -> None:
    _log(msg, level="WARN")


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
    _depth: int = 0,
) -> list[dict]:
    """フォルダ内の Google Docs を一覧取得する。"""
    indent = "  " * _depth
    _log(f"{indent}Drive API: フォルダ探索開始 folder_id={folder_id}")
    docs: list[dict] = []
    q = f"'{folder_id}' in parents and trashed=false"
    page_token = None
    page_num = 0

    while True:
        page_num += 1
        _log(f"{indent}Drive API: files.list ページ {page_num} 取得中...")
        resp = drive_service.files().list(
            q=q,
            spaces="drive",
            fields="nextPageToken,files(id,name,mimeType,createdTime)",
            pageSize=100,
            pageToken=page_token,
        ).execute()

        files = resp.get("files", [])
        doc_count = 0
        folder_count = 0
        for f in files:
            if f.get("mimeType") == _DOCS_MIME:
                docs.append(f)
                doc_count += 1
            elif recursive and f.get("mimeType") == _FOLDER_MIME:
                folder_count += 1
                _log(f"{indent}  サブフォルダ発見: {f.get('name', f['id'])}")
                sub_docs = list_docs_in_folder(
                    drive_service, f["id"], recursive=True, _depth=_depth + 1,
                )
                docs.extend(sub_docs)

        _log(f"{indent}Drive API: ページ {page_num} 完了 docs={doc_count} folders={folder_count}")

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    docs.sort(key=lambda x: x.get("createdTime", ""))
    _log(f"{indent}Drive API: フォルダ探索完了 合計 {len(docs)} docs")
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

    truncated = False
    if len(user_msg) > 150_000:
        user_msg = user_msg[:150_000] + "\n\n（以下省略）"
        truncated = True

    _log(f"  Claude API 呼び出し開始 model={model} input_chars={len(user_msg)}"
         + (" (truncated)" if truncated else ""))

    started = time.monotonic()
    resp = client.messages.create(
        model=model,
        max_tokens=4000,
        temperature=0,
        system=_build_extract_prompt(),
        messages=[{"role": "user", "content": user_msg}],
    )
    elapsed = time.monotonic() - started

    raw = ""
    for block in resp.content:
        if getattr(block, "type", "") == "text":
            raw += block.text

    usage = getattr(resp, "usage", None)
    in_tok = getattr(usage, "input_tokens", "?") if usage else "?"
    out_tok = getattr(usage, "output_tokens", "?") if usage else "?"
    _log(f"  Claude API 完了 elapsed={elapsed:.1f}s "
         f"input_tokens={in_tok} output_tokens={out_tok} response_chars={len(raw)}")

    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

    try:
        items = json.loads(raw)
    except json.JSONDecodeError as e:
        _log_warn(f"  JSON parse failed: {e} — doc={doc_name}")
        return []

    if not isinstance(items, list):
        _log_warn(f"  Claude output is not a list — doc={doc_name}")
        return []

    result = [str(item).strip() for item in items if str(item).strip()]
    _log(f"  抽出完了: {len(result)} 件のナレッジ候補")
    return result


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
        _log("マージ: 新規項目なし、スキップ")
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

    _log(f"マージ Claude API 呼び出し開始 既存={len(existing)}件 新規={len(new_items)}件 "
         f"payload_chars={len(payload)}")

    started = time.monotonic()
    resp = client.messages.create(
        model=model,
        max_tokens=4000,
        temperature=0,
        system=_build_merge_prompt(),
        messages=[{"role": "user", "content": payload}],
    )
    elapsed = time.monotonic() - started

    raw = ""
    for block in resp.content:
        if getattr(block, "type", "") == "text":
            raw += block.text

    usage = getattr(resp, "usage", None)
    in_tok = getattr(usage, "input_tokens", "?") if usage else "?"
    out_tok = getattr(usage, "output_tokens", "?") if usage else "?"
    _log(f"マージ Claude API 完了 elapsed={elapsed:.1f}s "
         f"input_tokens={in_tok} output_tokens={out_tok}")

    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        _log_warn("マージ JSON parse 失敗、連結リストを返します")
        return _normalize_knowledge_memos(existing + new_items)

    if not isinstance(items, list):
        _log_warn("マージ結果がリストではありません、連結リストを返します")
        return _normalize_knowledge_memos(existing + new_items)

    merged = [str(item).strip() for item in items if str(item).strip()]
    _log(f"マージ完了: {len(merged)} 件")
    return _normalize_knowledge_memos(merged)


def main() -> None:
    global _LOG_FILE

    parser = argparse.ArgumentParser(
        description="既存 Google Docs 議事録からナレッジを一括抽出し Knowledge Sheet に追加する"
    )
    parser.add_argument("--folder-id", required=True, help="Google Drive フォルダ ID")
    parser.add_argument("--dry-run", action="store_true", help="抽出結果を表示するだけで Sheet に書き込まない")
    parser.add_argument("--max-docs", type=int, default=0, help="処理する最大ドキュメント数（0=全件）")
    parser.add_argument("--recursive", action="store_true", help="サブフォルダも再帰的に探索する")
    parser.add_argument("--output-json", default=None, help="抽出結果を JSON ファイルに保存する")
    parser.add_argument("--log-file", default=None, help="ログをファイルにも出力する")
    args = parser.parse_args()

    if args.log_file:
        _LOG_FILE = args.log_file
        os.makedirs(os.path.dirname(_LOG_FILE) or ".", exist_ok=True)

    _log("=" * 60)
    _log("extract_knowledge_from_docs 開始")
    _log(f"  folder_id  = {args.folder_id}")
    _log(f"  dry_run    = {args.dry_run}")
    _log(f"  recursive  = {args.recursive}")
    _log(f"  max_docs   = {args.max_docs or '全件'}")
    _log(f"  output_json= {args.output_json or '(なし)'}")
    _log(f"  log_file   = {_LOG_FILE or '(なし)'}")
    _log(f"  sa_json    = {_sa_json_path()}")

    total_start = time.monotonic()

    _log("Google API 認証情報を構築中...")
    drive_service = _build_drive_service()
    docs_service = _build_docs_service()
    _log("Google API 認証完了")

    _log("フォルダ内ドキュメント一覧を取得中...")
    all_docs = list_docs_in_folder(drive_service, args.folder_id, recursive=args.recursive)
    _log(f"ドキュメント一覧取得完了: {len(all_docs)} 件の Google Docs を発見")

    if args.max_docs > 0:
        all_docs = all_docs[: args.max_docs]
        _log(f"--max-docs 指定により先頭 {len(all_docs)} 件のみ処理")

    if not all_docs:
        _log("処理対象のドキュメントが 0 件のため終了")
        return

    _log("-" * 60)
    _log("ドキュメント一覧:")
    for idx, d in enumerate(all_docs, 1):
        _log(f"  {idx:3d}. {d.get('name', d['id'])}  (created={d.get('createdTime', '?')})")
    _log("-" * 60)

    all_extracted: list[str] = []
    success_count = 0
    skip_count = 0
    error_count = 0

    for i, doc_meta in enumerate(all_docs, 1):
        doc_id = doc_meta["id"]
        doc_name = doc_meta.get("name", doc_id)
        _log(f"[{i}/{len(all_docs)}] 処理開始: {doc_name}")

        _log(f"  Docs API: テキスト取得中 doc_id={doc_id}")
        doc_start = time.monotonic()
        try:
            text = fetch_doc_text(docs_service, doc_id)
        except Exception as e:
            _log_error(f"  Docs API エラー: {e}")
            error_count += 1
            continue
        doc_elapsed = time.monotonic() - doc_start

        if not text.strip():
            _log(f"  スキップ: 空のドキュメント (取得={doc_elapsed:.1f}s)")
            skip_count += 1
            continue

        char_count = len(text)
        _log(f"  テキスト取得完了: {char_count:,} 文字 ({doc_elapsed:.1f}s)")

        try:
            items = extract_knowledge_from_text(text, doc_name=doc_name)
        except Exception as e:
            _log_error(f"  ナレッジ抽出エラー: {e}")
            error_count += 1
            continue

        if items:
            for item in items:
                _log(f"    ✓ {item}")
        else:
            _log("  抽出結果: 0 件（再利用価値のあるナレッジなし）")

        all_extracted.extend(items)
        success_count += 1

        cumulative = _normalize_knowledge_memos(all_extracted)
        _log(f"  [{i}/{len(all_docs)}] 処理完了: "
             f"今回={len(items)}件 累計ユニーク={len(cumulative)}件")

        if i < len(all_docs):
            time.sleep(1.0)

    all_extracted = _normalize_knowledge_memos(all_extracted)

    _log("=" * 60)
    _log("全ドキュメント処理完了")
    _log(f"  処理成功  : {success_count} 件")
    _log(f"  スキップ  : {skip_count} 件")
    _log(f"  エラー    : {error_count} 件")
    _log(f"  抽出ユニーク: {len(all_extracted)} 件")
    total_elapsed = time.monotonic() - total_start
    _log(f"  処理時間  : {total_elapsed:.1f}s")

    if args.output_json:
        os.makedirs(os.path.dirname(args.output_json) or ".", exist_ok=True)
        with open(args.output_json, "w", encoding="utf-8") as f:
            json.dump(all_extracted, f, ensure_ascii=False, indent=2)
        _log(f"JSON 保存完了: {args.output_json}")

    if args.dry_run:
        _log("[DRY RUN] 抽出されたナレッジ一覧:")
        for item in all_extracted:
            _log(f"  - {item}")
        _log(f"[DRY RUN] Knowledge Sheet への書き込みはスキップ")
        _log(f"完了 (total={total_elapsed:.1f}s)")
        return

    if not knowledge_store_enabled():
        _log_warn("KNOWLEDGE_SHEET_ID 未設定。Sheet に書き込めません。")
        _log("--output-json を使って結果をファイルに保存してください。")
        return

    _log("Knowledge Sheet から既存ナレッジを読み込み中...")
    sheet_start = time.monotonic()
    existing = load_knowledge_memos()
    _log(f"既存ナレッジ読み込み完了: {len(existing)} 件 ({time.monotonic() - sheet_start:.1f}s)")

    _log("既存ナレッジと新規抽出をマージ中...")
    merged = merge_knowledge_lists(existing, all_extracted)

    if merged == existing:
        _log("変更なし。Knowledge Sheet は最新の状態です。")
    else:
        _log(f"Knowledge Sheet に保存中... ({len(existing)} → {len(merged)} 件)")
        save_start = time.monotonic()
        save_knowledge_memos(merged)
        _log(f"Knowledge Sheet 保存完了 ({time.monotonic() - save_start:.1f}s)")

    _log("=" * 60)
    _log(f"完了！ {len(existing)} → {len(merged)} 件 (total={time.monotonic() - total_start:.1f}s)")


if __name__ == "__main__":
    main()
