"""Phase 2: Layer 2 統合知識(世界モデル)を構築する。

【処理フロー】
  [1] Drive folder「打合せ内容/過去」配下の Google Docs を全件列挙
  [2] 各 doc 本文を取得し、Sonnet で 3-5K字に縮約(並列、4 workers)
  [3] 既存 Knowledge Sheet の自由記述メモも入力に加える
  [4] Opus 4.7 に全部渡し、organizations/people/methods/voice の
      4セクションを JSON で一括生成
  [5] world_orgs / world_people / world_methods / world_voice の
      4タブに書き込み(全置換)
  [6] 既存 knowledge タブを knowledge_archived にリネーム
      (--no-archive を渡せばスキップ)

【実行方法】
  python phase2_build_world_model.py --folder-id <PAST_FOLDER_ID>
  python phase2_build_world_model.py --folder-id <ID> --max-docs 5  # 部分実行
  python phase2_build_world_model.py --folder-id <ID> --dry-run     # Sheet 書込しない

【コスト見積】
  Sonnet 縮約: 30件 × 入力20K字+出力3K字 ≈ $1.5
  Opus 統合: 1回 × 入力150K字+出力20K字 ≈ $3.0
  合計: ~$5/build。Phase 3 再咀嚼サイクル(月1回)でも同等。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
import time
from datetime import datetime
from typing import Any

import anthropic

from extract_knowledge_from_docs import (
    _build_docs_service,
    _build_drive_service,
    fetch_doc_text,
    list_docs_in_folder,
)
from knowledge_sheet_store import (
    knowledge_store_enabled,
    load_knowledge_memos,
)
from repo_env import load_dotenv_local
from world_knowledge_store import (
    WORLD_TABS,
    ensure_world_tabs,
    rename_tab,
    replace_tab_rows,
    world_store_enabled,
)

SONNET_MODEL = "claude-sonnet-4-20250514"
OPUS_MODEL = "claude-opus-4-7"
SUMMARIZE_MAX_TOKENS = 4000
SUMMARIZE_TIMEOUT_SEC = 180
SYNTHESIZE_MAX_TOKENS = 32000
SYNTHESIZE_TIMEOUT_SEC = 600
SUMMARIZE_PARALLEL = 4
CACHE_DIR = os.path.join("data", "knowledge", "_phase2_cache")


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _api_key() -> str:
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    return key


def _safe_filename(name: str) -> str:
    s = re.sub(r"[^\w\-_.()\s\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff]", "_", name)
    return s.strip()[:80] or "untitled"


def _cache_path(doc_id: str, kind: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"{doc_id}.{kind}.json")


def _build_summarize_system_prompt() -> str:
    return (
        "あなたは議事録から世界モデル構築用の情報を抽出するアシスタントです。"
        "渡された議事録本文から、後段で Opus が『企業・人物・手法・スタイル』の"
        "4ファイル世界モデルを構築できるよう、抽象化された再利用可能情報を抽出してください。"
        "\n\n【抽出ルール】"
        "\n- 単なる事実列挙ではなく、文脈と関連性が分かる形で書く"
        "\n- 同会議内の表記揺れ(嘱託再雇用者 ≠ 再雇用社員 等)があれば、正規表記を採用"
        "\n- 推測は禁止。本文に根拠がない情報は書かない"
        "\n- 出力は JSON のみ。前置き・コードフェンス禁止"
        "\n\n【出力スキーマ】"
        '\n{"meeting": {"date": "ファイル名や本文から推測される日付 YYYY-MM-DD",'
        ' "customer": "顧客企業名(社内会議なら空)",'
        ' "participants": ["参加者リスト"],'
        ' "topic": "会議の主議題(短い)"},'
        '\n "decisions": ["決定事項(原文に近い形)"],'
        '\n "next_actions": ["Next Action"],'
        '\n "open_issues": ["残論点"],'
        '\n "key_discussions": ["主要な議論ポイント(2-3文ずつ、文脈を含めて)"],'
        '\n "people_observations": [{"name": "人物名", "side": "customer|prosena", '
        '"role_or_org": "役職・所属", "observation": "発言の傾向・関心領域・関係性"}],'
        '\n "terms_and_methods": [{"term": "専門用語/手法名", '
        '"meaning_or_usage": "使われ方の説明"}],'
        '\n "asr_misrecognitions": [{"wrong": "誤", "right": "正", "context": "前後文"}],'
        '\n "aihara_voice_samples": ["相原氏が特徴的に使っている表現・言い回し"]}'
    )


def _call_sonnet_summarize(text: str, doc_meta: dict) -> dict[str, Any]:
    client = anthropic.Anthropic(api_key=_api_key())
    user_payload = {
        "doc_name": doc_meta.get("name", ""),
        "created_time": doc_meta.get("createdTime", ""),
        "body": text,
    }
    resp = client.messages.create(
        model=SONNET_MODEL,
        max_tokens=SUMMARIZE_MAX_TOKENS,
        temperature=0,
        timeout=SUMMARIZE_TIMEOUT_SEC,
        system=_build_summarize_system_prompt(),
        messages=[
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            {"role": "assistant", "content": "{"},
        ],
    )
    parts = []
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", "") == "text":
            parts.append(str(getattr(block, "text", "") or ""))
    raw = "\n".join(parts).strip()
    # assistant プレフィル "{" の補完
    if not raw.startswith("{"):
        raw = "{" + raw
    # コードフェンス除去
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 最初の { から最後の } を探す
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            raise
        return json.loads(m.group(0))


def _fetch_doc_text_with_retry(doc_id: str, name: str, max_retries: int = 3) -> str | None:
    """SSL/connection エラーに対するリトライ + スレッドローカル docs_service。

    googleapiclient の HTTP オブジェクトは thread-safe ではないため、スレッド毎に
    新規 docs_service を作る。SSLError / RemoteDisconnected は指数バックオフで再試行。
    """
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            local_service = _build_docs_service()
            return fetch_doc_text(local_service, doc_id)
        except Exception as e:  # noqa: BLE001
            last_err = e
            wait = 1.0 * (2 ** attempt)
            _log(f"  fetch_doc_text retry {attempt + 1}/{max_retries} for {name!r} in {wait:.0f}s: {e!r}")
            time.sleep(wait)
    _log(f"  fetch_doc_text giveup for {name!r}: {last_err!r}")
    return None


def summarize_one(doc: dict, docs_service) -> dict | None:
    """1 件の Google Doc を fetch + Sonnet 縮約。cache 有効。

    docs_service 引数は後方互換のため残してあるが内部では未使用
    (スレッドローカル service を毎回構築する)。
    """
    doc_id = doc["id"]
    name = doc.get("name", "")
    cache_path = _cache_path(doc_id, "summary")
    if os.path.isfile(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            cached["_doc_id"] = doc_id
            cached["_doc_name"] = name
            return cached
        except (OSError, json.JSONDecodeError):
            pass
    body = _fetch_doc_text_with_retry(doc_id, name)
    if body is None:
        return None
    if not body.strip():
        _log(f"  empty body: {name!r}")
        return None
    try:
        summary = _call_sonnet_summarize(body, {"name": name, "createdTime": doc.get("createdTime")})
    except Exception as e:
        _log(f"  sonnet summarize failed for {name!r}: {e!r}")
        return None
    summary["_doc_id"] = doc_id
    summary["_doc_name"] = name
    summary["_chars"] = len(body)
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
    except OSError as e:
        _log(f"  cache write failed: {e!r}")
    return summary


def _build_synthesize_system_prompt() -> str:
    return (
        "あなたは複数の議事録要約を統合して『世界モデル』を構築する専門アシスタントです。"
        "30件前後の議事録から抽出された構造化情報 + 既存の自由記述メモが渡されます。"
        "これらを統合して、新規ジョブの補正・要約プロンプトに inject される"
        "4種類の知識ファイルを生成してください。"
        "\n\n【統合の原則(極めて重要)】"
        "\n- **暗記ではなく統合学習**: 各議事録から抽出した個別事実をそのまま並べるのではなく、"
        "  企業・人物・手法ごとに**散文として再構成**し、関連性・時系列・変化を織り込む"
        "\n- **重複排除と矛盾解消**: 同じ人物・企業の情報が複数 doc にある場合は1セクションに統合"
        "\n  矛盾(例: 役職が異なる)は『2025年は○○、2026年は××に変更』のように時系列で書く"
        "\n- **抽象度のバランス**: 過度な要約も詳細列挙も避け、後で読み返して『なるほど』と"
        "  思える解説調にする"
        "\n- **創作禁止**: 入力に根拠のない情報は書かない"
        "\n- **表記正規化**: 表記揺れは正規表記(嘱託再雇用者、エンゲージメントサーベイ 等)に統一"
        "\n\n【出力スキーマ(必ず以下の JSON のみ、コードフェンス禁止)】"
        '\n{"organizations": ['
        '{"section_key": "企業名(他で参照される正規表記)",'
        ' "content_md": "## 概要\\n業種・規模・プレセナとの関係...\\n\\n## 進行中の案件\\n- ...\\n\\n## 主な人事側担当\\n- ...\\n\\n## 表記の正例(誤認識対策)\\n- 「正」(誤: 「誤」)"}'
        '],'
        '\n "people": ['
        '{"section_key": "人物名(姓のみ or フルネーム)",'
        ' "content_md": "**所属**: ...  \\n**側**: customer | prosena  \\n**役割**: ...  \\n**特徴**: 発言の傾向・関心領域...  \\n**他人物との関係**: ..."}'
        '],'
        '\n "methods": ['
        '{"section_key": "手法名/用語(Will-Can-Must, HRBP, エンゲージメントサーベイ 等)",'
        ' "content_md": "**プレセナでの位置づけ**: ...  \\n**使われ方**: ...  \\n**関連用語**: ..."}'
        '],'
        '\n "voice": ['
        '{"section_key": "セクション名(例: 『相原氏が好む言い回し』『Next Action の書き方』)",'
        ' "content_md": "..."}'
        ']}'
        "\n\n【セクション数の目安】"
        "\n- organizations: 5-15 (企業数に応じて、社内会議は『社内』にまとめる)"
        "\n- people: 10-30 (主要人物のみ、ワンショット人物は省略)"
        "\n- methods: 10-25 (頻出する手法・用語のみ)"
        "\n- voice: 3-8 (汎用的な書き方の傾向)"
    )


def _call_opus_synthesize(summaries: list[dict], existing_memos: list[str]) -> dict:
    client = anthropic.Anthropic(api_key=_api_key())
    user_payload = {
        "past_meeting_summaries": summaries,
        "existing_free_form_memos": existing_memos,
    }
    payload_text = json.dumps(user_payload, ensure_ascii=False)
    _log(f"  opus input chars: {len(payload_text)}")
    resp = client.messages.create(
        model=OPUS_MODEL,
        max_tokens=SYNTHESIZE_MAX_TOKENS,
        timeout=SYNTHESIZE_TIMEOUT_SEC,
        system=_build_synthesize_system_prompt(),
        messages=[{"role": "user", "content": payload_text}],
    )
    parts = []
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", "") == "text":
            parts.append(str(getattr(block, "text", "") or ""))
    raw = "\n".join(parts).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            raise
        return json.loads(m.group(0))


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 2 Layer 2 世界モデル構築")
    parser.add_argument(
        "--folder-id", required=True, help="過去議事録フォルダ ID (Drive)"
    )
    parser.add_argument(
        "--recursive", action="store_true", help="サブフォルダも探索する"
    )
    parser.add_argument(
        "--max-docs", type=int, default=0, help="処理する最大件数(0=全件)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Sheet 書き込みをスキップ"
    )
    parser.add_argument(
        "--no-archive", action="store_true",
        help="既存 knowledge タブを knowledge_archived にリネームしない",
    )
    parser.add_argument(
        "--include-memos", action="store_true", default=True,
        help="既存自由記述メモを入力に含める(default: True)",
    )
    parser.add_argument(
        "--skip-memos", action="store_true",
        help="既存メモを入力から除外する(--include-memos より優先)",
    )
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv_local()

    if not world_store_enabled():
        print("KNOWLEDGE_SHEET_ID が未設定です。中止。", file=sys.stderr)
        return 1

    _log("=== Phase 2 build start ===")

    # [1] Drive 列挙
    drive_service = _build_drive_service()
    docs_service = _build_docs_service()
    _log(f"列挙中: folder_id={args.folder_id} recursive={args.recursive}")
    docs = list_docs_in_folder(drive_service, args.folder_id, recursive=args.recursive)
    _log(f"  found {len(docs)} docs")
    if args.max_docs > 0:
        docs = docs[: args.max_docs]
        _log(f"  trimmed to {len(docs)} docs (max-docs)")

    if not docs:
        _log("docs が空です。中止。")
        return 1

    # [2] Sonnet 縮約 (並列)
    _log(f"=== Step 1/4: Sonnet 縮約 (parallel={SUMMARIZE_PARALLEL}) ===")
    summaries: list[dict] = []
    t0 = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=SUMMARIZE_PARALLEL) as ex:
        future_to_doc = {
            ex.submit(summarize_one, d, docs_service): d for d in docs
        }
        done = 0
        for fut in concurrent.futures.as_completed(future_to_doc):
            d = future_to_doc[fut]
            done += 1
            try:
                summary = fut.result()
            except Exception as e:
                _log(f"  [{done}/{len(docs)}] FAIL {d.get('name','?')}: {e!r}")
                continue
            if summary is None:
                _log(f"  [{done}/{len(docs)}] SKIP {d.get('name','?')}")
                continue
            summaries.append(summary)
            _log(f"  [{done}/{len(docs)}] ok {d.get('name','?')} chars_in={summary.get('_chars','?')}")
    _log(f"  完了: summaries={len(summaries)} elapsed={time.monotonic()-t0:.1f}s")

    if not summaries:
        _log("有効な縮約が0件。中止。")
        return 1

    # [3] 既存メモ取得
    existing_memos: list[str] = []
    if not args.skip_memos and args.include_memos:
        if knowledge_store_enabled():
            try:
                existing_memos = load_knowledge_memos() or []
                _log(f"既存メモ: {len(existing_memos)} 件 を入力に含める")
            except Exception as e:
                _log(f"既存メモ取得失敗: {e!r}")
        else:
            _log("knowledge_store_enabled=false (KNOWLEDGE_SHEET_ID 未設定?)")

    # [4] Opus 統合
    _log("=== Step 2/4: Opus 統合 ===")
    t0 = time.monotonic()
    try:
        world = _call_opus_synthesize(summaries, existing_memos)
    except Exception as e:
        _log(f"Opus 統合失敗: {e!r}")
        return 2
    _log(f"  Opus 完了 elapsed={time.monotonic()-t0:.1f}s")
    counts = {k: len(world.get(k, [])) for k in ("organizations", "people", "methods", "voice")}
    _log(f"  生成セクション: {counts}")

    # [5] Sheet 書き込み
    if args.dry_run:
        _log("=== Step 3/4: dry-run (Sheet 書き込みスキップ) ===")
        out_path = os.path.join(CACHE_DIR, "_dry_run_world.json")
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(world, f, ensure_ascii=False, indent=2)
        _log(f"  dry-run output: {out_path}")
    else:
        _log("=== Step 3/4: Sheet 書き込み ===")
        ensure_world_tabs()
        source_jobs = ", ".join(s.get("_doc_name", "") for s in summaries if s.get("_doc_name"))[:500]
        tab_data = [
            ("world_orgs", world.get("organizations") or []),
            ("world_people", world.get("people") or []),
            ("world_methods", world.get("methods") or []),
            ("world_voice", world.get("voice") or []),
        ]
        for tab, rows in tab_data:
            normalized: list[dict[str, Any]] = []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                key = str(r.get("section_key") or "").strip()
                body = str(r.get("content_md") or "").strip()
                if not key or not body:
                    continue
                normalized.append({
                    "section_key": key,
                    "content_md": body,
                    "source_jobs": source_jobs,
                })
            try:
                replace_tab_rows(tab, normalized)
                _log(f"  wrote {tab}: {len(normalized)} rows")
            except Exception as e:
                _log(f"  write failed for {tab}: {e!r}")
                return 3

    # [6] knowledge → knowledge_archived
    if not args.dry_run and not args.no_archive:
        _log("=== Step 4/4: knowledge タブをアーカイブ ===")
        if rename_tab("knowledge", "knowledge_archived"):
            _log("  knowledge -> knowledge_archived")
        else:
            _log(
                "  リネームスキップ(knowledge タブ未存在 or knowledge_archived 既存)"
            )

    _log("=== Phase 2 build complete ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
