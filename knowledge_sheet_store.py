import json
import os

import anthropic
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


KNOWLEDGE_SHEET_ID_ENV = "KNOWLEDGE_SHEET_ID"
KNOWLEDGE_SHEET_TAB_ENV = "KNOWLEDGE_SHEET_TAB"
GOOGLE_SERVICE_ACCOUNT_JSON_ENV = "GOOGLE_SERVICE_ACCOUNT_JSON"
DEFAULT_SERVICE_ACCOUNT_JSON_PATH = "credentials_service_account.json"
DEFAULT_KNOWLEDGE_SHEET_TAB = "knowledge"
KNOWLEDGE_UPDATER_MODEL = "claude-sonnet-4-20250514"


def _service_account_json_path() -> str:
    return os.getenv(
        GOOGLE_SERVICE_ACCOUNT_JSON_ENV,
        DEFAULT_SERVICE_ACCOUNT_JSON_PATH,
    ).strip()


def _knowledge_sheet_id() -> str:
    return os.getenv(KNOWLEDGE_SHEET_ID_ENV, "").strip()


def _knowledge_sheet_tab() -> str:
    return os.getenv(KNOWLEDGE_SHEET_TAB_ENV, DEFAULT_KNOWLEDGE_SHEET_TAB).strip() or DEFAULT_KNOWLEDGE_SHEET_TAB


def knowledge_store_enabled() -> bool:
    return bool(_knowledge_sheet_id())


def _load_anthropic_api_key() -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")
    return api_key


def _extract_text_from_anthropic(resp) -> str:
    texts: list[str] = []
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", "") == "text":
            texts.append(str(getattr(block, "text", "") or ""))
    return "\n".join(t for t in texts if t).strip()


def _extract_json_object(text: str) -> dict:
    raw = str(text or "").strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end < start:
        raise ValueError("json object not found")
    payload = json.loads(raw[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("parsed JSON is not an object")
    return payload


def _normalize_knowledge_memos(items: object) -> list[str]:
    if not isinstance(items, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = " ".join(str(item or "").strip().split())
        if not text:
            continue
        if text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


def _build_sheets_service():
    path = _service_account_json_path()
    if not os.path.isfile(path):
        raise FileNotFoundError(f"service account json not found: {path}")
    creds = ServiceAccountCredentials.from_service_account_file(
        path,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds)


def _get_tab_names(service, spreadsheet_id: str) -> set[str]:
    resp = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    sheets = resp.get("sheets", [])
    names: set[str] = set()
    for sheet in sheets:
        props = sheet.get("properties", {})
        title = str(props.get("title") or "").strip()
        if title:
            names.add(title)
    return names


def _ensure_sheet_tab(service, spreadsheet_id: str, tab_name: str) -> None:
    try:
        names = _get_tab_names(service, spreadsheet_id)
    except HttpError as e:
        raise RuntimeError(f"failed to read spreadsheet tabs: {e}") from e
    if tab_name in names:
        return
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]},
    ).execute()


def load_knowledge_memos() -> list[str]:
    spreadsheet_id = _knowledge_sheet_id()
    if not spreadsheet_id:
        return []
    service = _build_sheets_service()
    tab_name = _knowledge_sheet_tab()
    _ensure_sheet_tab(service, spreadsheet_id, tab_name)
    resp = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{tab_name}!A:A",
    ).execute()
    values = resp.get("values", [])
    flattened = [row[0] for row in values if isinstance(row, list) and row]
    return _normalize_knowledge_memos(flattened)


def save_knowledge_memos(memos: list[str]) -> None:
    spreadsheet_id = _knowledge_sheet_id()
    if not spreadsheet_id:
        raise RuntimeError("KNOWLEDGE_SHEET_ID is not set.")
    service = _build_sheets_service()
    tab_name = _knowledge_sheet_tab()
    _ensure_sheet_tab(service, spreadsheet_id, tab_name)
    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range=f"{tab_name}!A:A",
        body={},
    ).execute()
    normalized = _normalize_knowledge_memos(memos)
    if not normalized:
        return
    values = [[item] for item in normalized]
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{tab_name}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()


def _merge_knowledge_memos_with_claude(
    *,
    existing_memos: list[str],
    question_text: str,
    answer_text: str,
    model: str = KNOWLEDGE_UPDATER_MODEL,
) -> dict:
    client = anthropic.Anthropic(api_key=_load_anthropic_api_key())
    system_prompt = (
        "あなたは議事録AIの再利用ナレッジ管理アシスタントです。"
        "入力として、既存のナレッジメモ一覧と、今回ユーザーに確認した質問文、回答文が与えられます。"
        "目的は、今回の回答にジョブ横断で再利用価値があるかを判断し、"
        "既存ナレッジと重複・類似があれば統合整理したうえで、更新後のナレッジ一覧全体を返すことです。"
        "correction_dict のような置換辞書は作らず、1行1件の自由記述メモだけを管理してください。"
        "会議固有の一時的な yes/no 回答や、その場限りの数字確認のように再利用価値が低いものは追加しないでください。"
        "一方で、用語説明、役割定義、社内固有の呼称、サービス説明、関係性の説明などは蓄積対象にしてください。"
        "既存メモの意味が変わらない範囲で、より自然で再利用しやすい表現に統合して構いません。"
        "出力は JSON オブジェクトのみ。"
        '形式は {"updated_knowledge":["..."],"action":"unchanged|updated","reason":"string"} としてください。'
    )
    payload = {
        "existing_knowledge": existing_memos,
        "question_text": question_text,
        "answer_text": answer_text,
    }
    resp = client.messages.create(
        model=model,
        max_tokens=3000,
        temperature=0,
        system=system_prompt,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
    )
    parsed = _extract_json_object(_extract_text_from_anthropic(resp))
    updated = _normalize_knowledge_memos(parsed.get("updated_knowledge"))
    if not updated and existing_memos:
        updated = list(existing_memos)
    return {
        "updated_knowledge": updated,
        "action": str(parsed.get("action") or "").strip() or "unchanged",
        "reason": str(parsed.get("reason") or "").strip(),
    }


def merge_answer_into_knowledge_store(
    *,
    question_text: str,
    answer_text: str,
) -> dict:
    if not knowledge_store_enabled():
        return {
            "enabled": False,
            "updated": False,
            "reason": "knowledge_sheet_id_missing",
            "knowledge_count_before": 0,
            "knowledge_count_after": 0,
        }
    existing = load_knowledge_memos()
    merged = _merge_knowledge_memos_with_claude(
        existing_memos=existing,
        question_text=question_text,
        answer_text=answer_text,
    )
    updated = _normalize_knowledge_memos(merged.get("updated_knowledge"))
    changed = updated != existing
    if changed:
        save_knowledge_memos(updated)
    return {
        "enabled": True,
        "updated": changed,
        "reason": str(merged.get("reason") or "").strip(),
        "action": str(merged.get("action") or "").strip() or ("updated" if changed else "unchanged"),
        "knowledge_count_before": len(existing),
        "knowledge_count_after": len(updated),
    }


def format_knowledge_for_prompt(memos: list[str]) -> str:
    normalized = _normalize_knowledge_memos(memos)
    if not normalized:
        return ""
    lines = "\n".join(f"- {item}" for item in normalized)
    return (
        "\n\n【参考ナレッジ】以下は過去の確認回答から蓄積された補足知識です。\n"
        "文脈理解の参考にしてよいですが、入力本文にない事実を創作して補わないでください。\n"
        f"{lines}"
    )
