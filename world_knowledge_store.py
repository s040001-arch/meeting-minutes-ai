"""Layer 2 統合知識ストア (Phase 2)。

【目的】
- 過去議事録 + 既存自由記述メモを Opus で再咀嚼した「世界モデル」を
  Google Sheets の4タブに保存し、新規ジョブの補正・検出・要約プロンプトに
  関連セクションだけを inject する。

【データ構造】
Knowledge Sheet (KNOWLEDGE_SHEET_ID) 内に以下4タブを追加:
  - world_orgs:    A=企業名 | B=content_md | C=last_updated | D=source_jobs
  - world_people:  A=人物名 | B=content_md | C=last_updated | D=source_jobs
  - world_methods: A=用語   | B=content_md | C=last_updated | D=source_jobs
  - world_voice:   A=セクション名 | B=content_md | C=last_updated | D=source_jobs

【関連行検索】
- world_orgs / world_people は meeting_profile に応じてフィルタする
- world_methods / world_voice は全行常時 inject (相対的に小さい)

【既存 knowledge_sheet_store との関係】
- 既存自由記述メモは Phase 2 ビルドで吸収後、knowledge → knowledge_archived
  にリネーム。ランタイムは本ストアのみ参照する。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# 既存ストアと同じ環境変数を共有
KNOWLEDGE_SHEET_ID_ENV = "KNOWLEDGE_SHEET_ID"
GOOGLE_SERVICE_ACCOUNT_JSON_ENV = "GOOGLE_SERVICE_ACCOUNT_JSON"
DEFAULT_SERVICE_ACCOUNT_JSON_PATH = "credentials_service_account.json"

WORLD_TABS = ("world_orgs", "world_people", "world_methods", "world_voice")
HEADER_ROW = ["section_key", "content_md", "last_updated", "source_jobs"]

# 各タブのうち、ランタイムで全行を常時 inject するもの(相対的に小さい)
ALWAYS_INJECT_TABS = ("world_methods", "world_voice")

# プロンプトに含める最大文字数 (安全側、超過時は truncate)
MAX_INJECTION_CHARS = 16000

# プロセスメモリキャッシュ TTL(秒)。同一ジョブの複数ステップ呼び出しで Sheet API
# を叩き直さないため。チャンク補正は十秒〜数十秒スパンで多数回呼び出される。
_CACHE_TTL_SEC = 300
_cached_world_tabs: dict[str, list[dict[str, str]]] | None = None
_cached_at: float = 0.0


def invalidate_world_cache() -> None:
    """書き込み後やテストで明示的にキャッシュを無効化する。"""
    global _cached_world_tabs, _cached_at
    _cached_world_tabs = None
    _cached_at = 0.0


def _service_account_json_path() -> str:
    return os.getenv(GOOGLE_SERVICE_ACCOUNT_JSON_ENV, "").strip() or DEFAULT_SERVICE_ACCOUNT_JSON_PATH


def _knowledge_sheet_id() -> str:
    return os.getenv(KNOWLEDGE_SHEET_ID_ENV, "").strip()


def world_store_enabled() -> bool:
    return bool(_knowledge_sheet_id())


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
    names: set[str] = set()
    for sheet in resp.get("sheets", []):
        title = str(sheet.get("properties", {}).get("title") or "").strip()
        if title:
            names.add(title)
    return names


def _ensure_tab_with_header(service, spreadsheet_id: str, tab: str) -> None:
    names = _get_tab_names(service, spreadsheet_id)
    if tab not in names:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
        ).execute()
    # ヘッダ行を投入(なければ)
    resp = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"{tab}!A1:D1"
    ).execute()
    if not resp.get("values"):
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{tab}!A1",
            valueInputOption="RAW",
            body={"values": [HEADER_ROW]},
        ).execute()


def ensure_world_tabs() -> None:
    """4タブをまとめて存在保証する(初期セットアップ + ビルド時の冒頭で呼ぶ)。"""
    sheet_id = _knowledge_sheet_id()
    if not sheet_id:
        raise RuntimeError("KNOWLEDGE_SHEET_ID is not set.")
    service = _build_sheets_service()
    for tab in WORLD_TABS:
        _ensure_tab_with_header(service, sheet_id, tab)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _normalize_row(row: list[str]) -> dict[str, str]:
    """[section_key, content_md, last_updated, source_jobs] → dict"""
    padded = list(row) + [""] * (len(HEADER_ROW) - len(row))
    return {
        "section_key": str(padded[0] or "").strip(),
        "content_md": str(padded[1] or ""),
        "last_updated": str(padded[2] or "").strip(),
        "source_jobs": str(padded[3] or "").strip(),
    }


def load_tab(tab: str) -> list[dict[str, str]]:
    """指定タブの全データ行を返す(ヘッダを除く)。"""
    sheet_id = _knowledge_sheet_id()
    if not sheet_id:
        return []
    service = _build_sheets_service()
    try:
        resp = service.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"{tab}!A2:D"
        ).execute()
    except HttpError as e:
        print(f"world_knowledge_load_tab_failed tab={tab} err={e!r}")
        return []
    values = resp.get("values", [])
    out: list[dict[str, str]] = []
    for row in values:
        if not isinstance(row, list):
            continue
        norm = _normalize_row(row)
        if not norm["section_key"] or not norm["content_md"]:
            continue
        out.append(norm)
    return out


def load_all_world_tabs(use_cache: bool = True) -> dict[str, list[dict[str, str]]]:
    """4タブを batchGet で1回取得する(往復1回)。

    use_cache=True (default): プロセスメモリキャッシュを使う(TTL 300s)。
    """
    global _cached_world_tabs, _cached_at
    now = time.monotonic()
    if use_cache and _cached_world_tabs is not None and (now - _cached_at) < _CACHE_TTL_SEC:
        return _cached_world_tabs

    sheet_id = _knowledge_sheet_id()
    if not sheet_id:
        out_empty = {tab: [] for tab in WORLD_TABS}
        _cached_world_tabs = out_empty
        _cached_at = now
        return out_empty
    service = _build_sheets_service()
    ranges = [f"{tab}!A2:D" for tab in WORLD_TABS]
    try:
        resp = service.spreadsheets().values().batchGet(
            spreadsheetId=sheet_id, ranges=ranges
        ).execute()
    except HttpError as e:
        print(f"world_knowledge_batch_get_failed err={e!r}")
        return {tab: [] for tab in WORLD_TABS}
    value_ranges = resp.get("valueRanges", [])
    out: dict[str, list[dict[str, str]]] = {tab: [] for tab in WORLD_TABS}
    for tab, vr in zip(WORLD_TABS, value_ranges):
        rows = vr.get("values", []) if isinstance(vr, dict) else []
        for row in rows:
            if not isinstance(row, list):
                continue
            norm = _normalize_row(row)
            if not norm["section_key"] or not norm["content_md"]:
                continue
            out[tab].append(norm)
    _cached_world_tabs = out
    _cached_at = now
    return out


def _invalidate_after_write(_tab: str) -> None:
    invalidate_world_cache()


def replace_tab_rows(tab: str, rows: list[dict[str, Any]]) -> None:
    """指定タブのデータ行を全置換する。

    rows: [{"section_key":..., "content_md":..., "source_jobs":[...] or str}]
    """
    sheet_id = _knowledge_sheet_id()
    if not sheet_id:
        raise RuntimeError("KNOWLEDGE_SHEET_ID is not set.")
    service = _build_sheets_service()
    _ensure_tab_with_header(service, sheet_id, tab)
    # ヘッダを残してデータ行をクリア
    service.spreadsheets().values().clear(
        spreadsheetId=sheet_id, range=f"{tab}!A2:D", body={}
    ).execute()
    if not rows:
        return
    now = _now_iso()
    out_rows: list[list[str]] = []
    for r in rows:
        key = str(r.get("section_key") or "").strip()
        body = str(r.get("content_md") or "").strip()
        if not key or not body:
            continue
        src = r.get("source_jobs")
        if isinstance(src, (list, tuple)):
            src_str = ", ".join(str(s) for s in src if s)
        else:
            src_str = str(src or "").strip()
        out_rows.append([key, body, r.get("last_updated") or now, src_str])
    if not out_rows:
        return
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{tab}!A2",
        valueInputOption="RAW",
        body={"values": out_rows},
    ).execute()
    _invalidate_after_write(tab)


def rename_tab(old_name: str, new_name: str) -> bool:
    """タブをリネームする(既存メモのアーカイブ用)。

    存在しない or 既に new_name 側が存在する場合は False を返す。
    """
    sheet_id = _knowledge_sheet_id()
    if not sheet_id:
        return False
    service = _build_sheets_service()
    resp = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    sheets = resp.get("sheets", [])
    sheet_id_to_rename: int | None = None
    existing_names: set[str] = set()
    for sh in sheets:
        props = sh.get("properties", {})
        name = str(props.get("title") or "").strip()
        existing_names.add(name)
        if name == old_name:
            sheet_id_to_rename = props.get("sheetId")
    if sheet_id_to_rename is None:
        return False
    if new_name in existing_names:
        return False
    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={
            "requests": [{
                "updateSheetProperties": {
                    "properties": {"sheetId": sheet_id_to_rename, "title": new_name},
                    "fields": "title",
                }
            }]
        },
    ).execute()
    return True


# ============================================================
# ランタイム: 関連セクション検索 + プロンプト整形
# ============================================================


def _name_matches(row_key: str, target_names: list[str]) -> bool:
    """row の section_key が target_names のいずれかと部分一致するか。"""
    if not target_names:
        return False
    rk = row_key.strip()
    for n in target_names:
        n2 = (n or "").strip()
        if not n2:
            continue
        if n2 in rk or rk in n2:
            return True
        # 「○○様」「○○さん」など敬称を除いた一致
        for suffix in ("様", "さん", "氏"):
            if n2.endswith(suffix):
                base = n2[: -len(suffix)]
                if base and (base in rk or rk in base):
                    return True
    return False


def fetch_relevant_world_sections(
    *,
    customer_name: str = "",
    participants: list[str] | None = None,
    world_tabs: dict[str, list[dict[str, str]]] | None = None,
) -> dict[str, list[dict[str, str]]]:
    """meeting_profile に基づき関連 Layer 2 行を返す。

    返り値: tab -> [row, ...]
    """
    if world_tabs is None:
        world_tabs = load_all_world_tabs()

    customer = (customer_name or "").strip()
    parts = [p for p in (participants or []) if (p or "").strip()]

    rel: dict[str, list[dict[str, str]]] = {tab: [] for tab in WORLD_TABS}

    # orgs: 顧客名と一致する行
    if customer:
        for row in world_tabs.get("world_orgs", []):
            if _name_matches(row["section_key"], [customer]):
                rel["world_orgs"].append(row)

    # people: 参加者名のいずれかと一致する行 + 既知の顧客側参加者を芋づる
    if parts:
        for row in world_tabs.get("world_people", []):
            if _name_matches(row["section_key"], parts):
                rel["world_people"].append(row)
            # content_md 内にも参加者名がある行は関連と見なす(顧客×人物クロスリファレンス)
            elif customer and customer in row.get("content_md", ""):
                # 顧客行で参加者言及があれば追加(過剰検出防止のため最大3件)
                if len(rel["world_people"]) < 3:
                    rel["world_people"].append(row)

    # methods / voice: 全行 inject
    rel["world_methods"] = list(world_tabs.get("world_methods", []))
    rel["world_voice"] = list(world_tabs.get("world_voice", []))

    return rel


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 30].rstrip() + "\n\n[... 以下省略 ...]"


def format_world_for_prompt(
    relevant: dict[str, list[dict[str, str]]],
    *,
    purpose: str = "correction",
) -> str:
    """関連 Layer 2 行群を Claude プロンプトに inject するテキストへ整形。

    purpose: "correction" | "detection" | "minutes" | "coherence"
        プロンプト末尾の解釈ガイダンス文を切り替える。
    """
    section_titles = {
        "world_orgs": "## 関連企業・案件の理解",
        "world_people": "## 関連人物の理解",
        "world_methods": "## プレセナの手法・用語",
        "world_voice": "## 議事録のスタイル(相原氏の好み)",
    }
    blocks: list[str] = []
    for tab in WORLD_TABS:
        rows = relevant.get(tab, []) or []
        if not rows:
            continue
        body_parts: list[str] = []
        for r in rows:
            key = r["section_key"]
            md = r["content_md"].strip()
            body_parts.append(f"### {key}\n\n{md}")
        body = "\n\n".join(body_parts)
        blocks.append(f"{section_titles[tab]}\n\n{body}")

    if not blocks:
        return ""

    body = "\n\n".join(blocks)
    body = _truncate(body, MAX_INJECTION_CHARS)

    if purpose == "detection":
        guidance = (
            "上記は過去議事録から蓄積された世界モデル(関連企業・人物・用語)です。"
            "ここに記載された誤認識パターンや専門用語が入力に出現した場合は、"
            "積極的に検出対象として扱ってください。"
        )
    elif purpose == "minutes":
        guidance = (
            "上記の世界モデルを参考に、議事録のセクション(議題・決定・残論点・"
            "Next Action)を作成してください。世界モデルにない事実を創作してはなりません。"
        )
    elif purpose == "coherence":
        guidance = (
            "上記の世界モデルを参考に、入力文書中の違和感(造語・誤認識・破綻箇所)を"
            "検出してください。世界モデルの表記に沿って正解語を推定すると精度が上がります。"
        )
    else:
        guidance = (
            "上記の世界モデルは過去議事録から蓄積した参考情報です。"
            "文脈理解と表記揃えに使ってよいですが、入力本文にない事実を創作して補ってはいけません。"
        )

    return f"\n\n【世界モデル(過去議事録由来)】\n\n{body}\n\n{guidance}"


def get_combined_runtime_knowledge(
    *,
    customer_name: str = "",
    participants: list[str] | None = None,
    purpose: str = "correction",
) -> str:
    """1回の呼び出しで関連 Layer 2 を取得→整形して返す便利関数。

    新規ジョブの prompt 構築箇所から本関数を呼ぶ。
    """
    if not world_store_enabled():
        return ""
    try:
        all_tabs = load_all_world_tabs()
    except Exception as e:  # noqa: BLE001
        print(f"world_knowledge_runtime_load_failed={e!r}")
        return ""
    rel = fetch_relevant_world_sections(
        customer_name=customer_name,
        participants=participants,
        world_tabs=all_tabs,
    )
    return format_world_for_prompt(rel, purpose=purpose)


def get_runtime_knowledge_block(
    *,
    meeting_profile: dict | None = None,
    purpose: str = "correction",
) -> str:
    """Layer 2 をまず試行し、空ならレガシー free-form memos にフォールバック。

    全 inject 箇所はこの統合関数を呼ぶことで、Phase 2 移行期 (Layer 2 未構築)も
    旧来のメモが効くようにする。Phase 2 完了後は Layer 2 のみが返る。
    """
    profile = meeting_profile or {}
    customer = str(profile.get("customer_name") or "").strip()
    parts = profile.get("participants") or []
    # Layer 2 を試す
    layer2_text = ""
    if world_store_enabled():
        try:
            layer2_text = get_combined_runtime_knowledge(
                customer_name=customer, participants=parts, purpose=purpose,
            )
        except Exception as e:  # noqa: BLE001
            print(f"world_knowledge_runtime_fetch_failed={e!r}")
    if layer2_text.strip():
        return layer2_text
    # Fallback: 既存自由記述メモ(Phase 2 アーカイブ前 or Layer 2 が空)
    try:
        from knowledge_sheet_store import format_knowledge_for_prompt, load_knowledge_memos
        memos = load_knowledge_memos() or []
        if memos:
            return format_knowledge_for_prompt(memos)
    except Exception as e:  # noqa: BLE001
        print(f"legacy_knowledge_fallback_failed={e!r}")
    return ""


# ============================================================
# CLI (デバッグ用)
# ============================================================


def _main_cli() -> int:
    import argparse
    import sys

    try:
        from repo_env import load_dotenv_local
        load_dotenv_local()
    except Exception:  # noqa: BLE001
        pass

    parser = argparse.ArgumentParser(description="Layer 2 世界モデル CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="新4タブを作成(ヘッダ行のみ)")
    sub.add_parser("list", help="全タブの section_key 一覧を表示")

    p_show = sub.add_parser("show", help="特定タブの全行を表示")
    p_show.add_argument("tab", choices=WORLD_TABS)

    p_query = sub.add_parser("query", help="関連行検索(顧客名・参加者で)")
    p_query.add_argument("--customer", default="")
    p_query.add_argument("--participants", default="", help="カンマ区切り")
    p_query.add_argument(
        "--purpose", default="correction",
        choices=("correction", "detection", "minutes", "coherence"),
    )

    p_rename = sub.add_parser("rename-tab", help="タブをリネーム(アーカイブ用)")
    p_rename.add_argument("old")
    p_rename.add_argument("new")

    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    if args.cmd == "init":
        ensure_world_tabs()
        print(f"ensured tabs: {', '.join(WORLD_TABS)}")
        return 0

    if args.cmd == "list":
        for tab in WORLD_TABS:
            rows = load_tab(tab)
            print(f"\n[{tab}] {len(rows)} rows")
            for r in rows:
                print(f"  - {r['section_key']}  ({len(r['content_md'])} chars)")
        return 0

    if args.cmd == "show":
        rows = load_tab(args.tab)
        for r in rows:
            print(f"\n=== {r['section_key']} (updated={r['last_updated']}) ===")
            print(r["content_md"])
            print()
        return 0

    if args.cmd == "query":
        parts = [p.strip() for p in args.participants.split(",") if p.strip()]
        text = get_combined_runtime_knowledge(
            customer_name=args.customer, participants=parts, purpose=args.purpose
        )
        print(text or "(empty)")
        return 0

    if args.cmd == "rename-tab":
        ok = rename_tab(args.old, args.new)
        print(f"renamed={ok}  {args.old} -> {args.new}")
        return 0 if ok else 1

    return 1


if __name__ == "__main__":
    raise SystemExit(_main_cli())
