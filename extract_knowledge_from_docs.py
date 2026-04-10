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

from repo_env import load_dotenv_local

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
_DETAIL_PAD = 52


# ---------------------------------------------------------------------------
# ログ関数
# ---------------------------------------------------------------------------

def _log(msg: str, detail: str = "") -> None:
    """わかりやすいメッセージを左側、技術情報を右側に表示する。"""
    ts = datetime.now().strftime("%H:%M:%S")
    if detail:
        padded = msg.ljust(_DETAIL_PAD)
        line = f"  [{ts}] {padded} | {detail}"
    else:
        line = f"  [{ts}] {msg}"
    print(line, flush=True)
    _log_to_file(line)


def _log_heading(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"\n  [{ts}] === {msg} ==="
    print(line, flush=True)
    _log_to_file(line)


def _log_item(msg: str) -> None:
    line = f"           - {msg}"
    print(line, flush=True)
    _log_to_file(line)


def _log_progress(elapsed_sec: float, chars: int) -> None:
    """AI処理中の経過表示（同じ行を上書きしない版）。"""
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"  [{ts}]   ...処理中（{elapsed_sec:.0f}秒経過、{chars:,}文字受信）"
    print(line, flush=True)
    _log_to_file(line)


def _log_error(msg: str, detail: str = "") -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    if detail:
        padded = msg.ljust(_DETAIL_PAD)
        line = f"  [{ts}] *** エラー: {padded} | {detail}"
    else:
        line = f"  [{ts}] *** エラー: {msg}"
    print(line, flush=True, file=sys.stderr)
    _log_to_file(line)


def _log_warn(msg: str, detail: str = "") -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    if detail:
        padded = msg.ljust(_DETAIL_PAD)
        line = f"  [{ts}] * 注意: {padded} | {detail}"
    else:
        line = f"  [{ts}] * 注意: {msg}"
    print(line, flush=True)
    _log_to_file(line)


def _log_to_file(line: str) -> None:
    if _LOG_FILE:
        try:
            with open(_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line.rstrip() + "\n")
        except OSError:
            pass


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


# ---------------------------------------------------------------------------
# Google Drive / Docs
# ---------------------------------------------------------------------------

def list_docs_in_folder(
    drive_service,
    folder_id: str,
    *,
    recursive: bool = False,
    _depth: int = 0,
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
                fname = f.get("name", f["id"])
                if _depth == 0:
                    _log(f"サブフォルダを探索中: {fname}")
                sub_docs = list_docs_in_folder(
                    drive_service, f["id"], recursive=True, _depth=_depth + 1,
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


# ---------------------------------------------------------------------------
# Claude プロンプト
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Claude API（ストリーミングでプログレス表示）
# ---------------------------------------------------------------------------

def _call_claude_with_progress(
    client: anthropic.Anthropic,
    *,
    model: str,
    max_tokens: int,
    system: str,
    user_message: str,
    progress_label: str = "AI処理",
    progress_interval_sec: float = 5.0,
) -> tuple[str, int, int]:
    """ストリーミング API で呼び出し、処理中に経過を表示する。

    Returns: (response_text, input_tokens, output_tokens)
    """
    full_response = ""
    started = time.monotonic()
    last_progress = started
    input_tokens = 0
    output_tokens = 0

    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        for chunk in stream.text_stream:
            full_response += chunk
            now = time.monotonic()
            if now - last_progress >= progress_interval_sec:
                _log_progress(now - started, len(full_response))
                last_progress = now

    elapsed = time.monotonic() - started

    final_msg = getattr(stream, "get_final_message", lambda: None)()
    if final_msg:
        usage = getattr(final_msg, "usage", None)
        if usage:
            input_tokens = getattr(usage, "input_tokens", 0)
            output_tokens = getattr(usage, "output_tokens", 0)

    _log(
        f"{progress_label}完了（{elapsed:.0f}秒）",
        f"tokens: {input_tokens}→{output_tokens}, {len(full_response):,}文字",
    )
    return full_response, input_tokens, output_tokens


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

    detail = f"model={model}, {len(user_msg):,}文字"
    if truncated:
        detail += " (長いため一部省略)"
    _log("AIでナレッジを抽出中...", detail)

    raw, _, _ = _call_claude_with_progress(
        client,
        model=model,
        max_tokens=4000,
        system=_build_extract_prompt(),
        user_message=user_msg,
        progress_label="AI抽出",
    )

    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

    try:
        items = json.loads(raw)
    except json.JSONDecodeError as e:
        _log_warn(f"AIの応答を解析できませんでした", f"doc={doc_name}, error={e}")
        return []

    if not isinstance(items, list):
        _log_warn(f"AIの応答が想定外の形式でした", f"doc={doc_name}")
        return []

    return [str(item).strip() for item in items if str(item).strip()]


def merge_knowledge_lists(
    existing: list[str],
    new_items: list[str],
    *,
    model: str = _MERGE_MODEL,
) -> list[str]:
    """既存ナレッジと新規抽出をマージする。"""
    if not new_items:
        _log("新規ナレッジなし、マージ不要")
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

    _log(
        f"AIで重複整理中（既存{len(existing)}件＋新規{len(new_items)}件）...",
        f"payload={len(payload):,}文字",
    )

    raw, _, _ = _call_claude_with_progress(
        client,
        model=model,
        max_tokens=4000,
        system=_build_merge_prompt(),
        user_message=payload,
        progress_label="マージ",
    )

    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        _log_warn("マージ結果を解析できませんでした。単純結合で代替します")
        return _normalize_knowledge_memos(existing + new_items)

    if not isinstance(items, list):
        _log_warn("マージ結果が想定外の形式でした。単純結合で代替します")
        return _normalize_knowledge_memos(existing + new_items)

    merged = [str(item).strip() for item in items if str(item).strip()]
    return _normalize_knowledge_memos(merged)


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------

def main() -> None:
    global _LOG_FILE
    load_dotenv_local()

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

    total_start = time.monotonic()

    _log_heading("ナレッジ一括抽出を開始します")
    _log("対象フォルダ", f"id={args.folder_id}")
    _log(f"モード: {'確認のみ（書き込みなし）' if args.dry_run else '本番（Sheet に書き込み）'}")
    if args.max_docs:
        _log(f"処理上限: 先頭 {args.max_docs} 件のみ")
    if args.recursive:
        _log("サブフォルダも含めて探索します")

    # --- Google API 認証 ---
    _log("Google に接続中...")
    try:
        drive_service = _build_drive_service()
        docs_service = _build_docs_service()
    except Exception as e:
        _log_error("Google への接続に失敗しました", str(e))
        sys.exit(1)
    _log("Google に接続しました")

    # --- ドキュメント一覧 ---
    _log("フォルダ内の議事録を探しています...")
    all_docs = list_docs_in_folder(drive_service, args.folder_id, recursive=args.recursive)
    _log(f"{len(all_docs)} 件の議事録を見つけました")

    if args.max_docs > 0:
        all_docs = all_docs[: args.max_docs]
        _log(f"先頭 {len(all_docs)} 件を処理します")

    if not all_docs:
        _log("処理対象の議事録が見つかりませんでした。終了します")
        return

    _log_heading(f"議事録一覧（{len(all_docs)}件）")
    for idx, d in enumerate(all_docs, 1):
        created = d.get("createdTime", "?")[:10]
        _log(f"  {idx:3d}. {d.get('name', '(名前なし)')}", f"作成日={created}")

    # --- ドキュメントごとの処理 ---
    all_extracted: list[str] = []
    success_count = 0
    skip_count = 0
    error_count = 0

    for i, doc_meta in enumerate(all_docs, 1):
        doc_id = doc_meta["id"]
        doc_name = doc_meta.get("name", doc_id)

        _log_heading(f"[{i}/{len(all_docs)}] {doc_name}")

        # テキスト取得
        _log("議事録のテキストを取得中...")
        doc_start = time.monotonic()
        try:
            text = fetch_doc_text(docs_service, doc_id)
        except Exception as e:
            _log_error("テキスト取得に失敗しました", str(e))
            error_count += 1
            continue
        doc_elapsed = time.monotonic() - doc_start

        if not text.strip():
            _log("空の議事録のためスキップします", f"{doc_elapsed:.1f}秒")
            skip_count += 1
            continue

        _log(f"テキスト取得完了（{len(text):,}文字）", f"{doc_elapsed:.1f}秒")

        # ナレッジ抽出
        try:
            items = extract_knowledge_from_text(text, doc_name=doc_name)
        except Exception as e:
            _log_error("ナレッジ抽出に失敗しました", str(e))
            error_count += 1
            continue

        if items:
            _log(f"{len(items)} 件のナレッジを発見:")
            for item in items:
                _log_item(item)
        else:
            _log("再利用できるナレッジは見つかりませんでした")

        all_extracted.extend(items)
        success_count += 1

        cumulative = _normalize_knowledge_memos(all_extracted)
        _log(
            f"[{i}/{len(all_docs)}] 完了",
            f"今回={len(items)}件, 累計ユニーク={len(cumulative)}件",
        )

        if i < len(all_docs):
            time.sleep(1.0)

    # --- 集計 ---
    all_extracted = _normalize_knowledge_memos(all_extracted)
    total_elapsed = time.monotonic() - total_start

    _log_heading("処理結果サマリー")
    _log(f"処理した議事録: {success_count} 件")
    if skip_count:
        _log(f"スキップ: {skip_count} 件（空の議事録）")
    if error_count:
        _log(f"エラー: {error_count} 件")
    _log(f"抽出したナレッジ: {len(all_extracted)} 件（重複除去済み）")
    _log(f"所要時間: {total_elapsed:.0f}秒（{total_elapsed/60:.1f}分）")

    # --- JSON 保存 ---
    if args.output_json:
        os.makedirs(os.path.dirname(args.output_json) or ".", exist_ok=True)
        with open(args.output_json, "w", encoding="utf-8") as f:
            json.dump(all_extracted, f, ensure_ascii=False, indent=2)
        _log(f"JSON ファイルに保存しました", args.output_json)

    # --- Sheet 書き込み ---
    if args.dry_run:
        _log_heading("確認モード（書き込みなし）")
        _log("抽出されたナレッジ一覧:")
        for item in all_extracted:
            _log_item(item)
        _log("Knowledge Sheet への書き込みはスキップしました")
        _log(f"完了（合計 {total_elapsed:.0f}秒）")
        return

    if not knowledge_store_enabled():
        _log_warn("KNOWLEDGE_SHEET_ID が未設定のため Sheet に書き込めません")
        _log("--output-json で結果をファイルに保存してください")
        return

    _log_heading("Knowledge Sheet を更新中")

    _log("既存のナレッジを Sheet から読み込み中...")
    sheet_start = time.monotonic()
    existing = load_knowledge_memos()
    _log(f"既存ナレッジ: {len(existing)} 件", f"{time.monotonic() - sheet_start:.1f}秒")

    merged = merge_knowledge_lists(existing, all_extracted)

    if merged == existing:
        _log("変更なし。Knowledge Sheet は最新の状態です")
    else:
        _log(f"Sheet に保存中（{len(existing)}件 → {len(merged)}件）...")
        save_start = time.monotonic()
        save_knowledge_memos(merged)
        _log(f"Sheet 保存完了", f"{time.monotonic() - save_start:.1f}秒")

    _log_heading("完了")
    _log(f"Knowledge Sheet: {len(existing)}件 → {len(merged)}件")
    _log(f"合計所要時間: {total_elapsed:.0f}秒（{total_elapsed/60:.1f}分）")


if __name__ == "__main__":
    main()
