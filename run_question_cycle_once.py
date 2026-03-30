import argparse
import json
import os
import uuid
from datetime import datetime, timezone

import requests

from generate_one_question import TYPE_PRIORITY, load_unknown_points
from line_send_question import build_line_message, push_line_message
from question_value_selection import (
    deduplicate_unknown_points_by_type_text,
    format_top_candidates_debug,
    pop_value_fields,
    select_one_unknown_value_based,
)
from repo_env import load_dotenv_local

LINE_PENDING_CONTEXT_PATH = os.path.join("data", "line_pending_context.json")


def resolve_context_text_path(job_id: str, input_root: str, explicit_path: str | None) -> str | None:
    if explicit_path:
        return explicit_path if os.path.isfile(explicit_path) else None
    job_dir = os.path.join(input_root, job_id)
    for name in (
        "merged_transcript_after_qa.txt",
        "merged_transcript_ai.txt",
        "merged_transcript.txt",
    ):
        p = os.path.join(job_dir, name)
        if os.path.isfile(p):
            return p
    return None


RISKY_TYPE_PRIORITY = {
    "proper_noun_candidate": "固有名詞",
    "organization_candidate": "固有名詞",
    "service_candidate": "固有名詞",
    "suspicious_word": "固有名詞",
    "suspicious_number_or_role": "数値",
}


def select_one_unknown_prioritized(unknown_points: list[dict]) -> tuple[dict, dict]:
    if not unknown_points:
        raise ValueError("unknown points is empty.")

    def key_func(idx_item: tuple[int, dict]) -> tuple[int, int, int]:
        idx, item = idx_item
        raw_type = str(item.get("type", ""))
        mapped = RISKY_TYPE_PRIORITY.get(raw_type, raw_type)
        # risky_terms系を先に評価
        risky_band = 0 if raw_type in RISKY_TYPE_PRIORITY else 1
        base_priority = TYPE_PRIORITY.get(mapped, 999)
        # 同順位は入力順を維持
        return (risky_band, base_priority, idx)

    idx, selected = min(enumerate(unknown_points), key=key_func)
    raw_type = str(selected.get("type", ""))
    risky_band = 0 if raw_type in RISKY_TYPE_PRIORITY else 1
    mapped = RISKY_TYPE_PRIORITY.get(raw_type, raw_type)
    base_priority = TYPE_PRIORITY.get(mapped, 999)
    audit = {
        "index_in_unknown_points": idx,
        "unknown_points_count": len(unknown_points),
        "type_raw": raw_type,
        "risky_band": risky_band,
        "type_priority_rank": base_priority,
    }
    return selected, audit


def write_line_pending_context(
    job_id: str,
    question_id: str,
    question_text: str,
    selected_unknown: dict,
    selection_audit: dict,
) -> None:
    """webhook が回答に job_id を付与できるよう、直近の質問コンテキストを共有ファイルへ書く。"""
    payload = {
        "written_at": datetime.now(timezone.utc).isoformat(),
        "job_id": job_id,
        "question_id": question_id,
        "question_text": question_text,
        "selected_unknown": selected_unknown,
        "selection_audit": selection_audit,
    }
    os.makedirs(os.path.dirname(LINE_PENDING_CONTEXT_PATH) or ".", exist_ok=True)
    with open(LINE_PENDING_CONTEXT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    _sync_line_pending_to_remote(payload)


def _sync_line_pending_to_remote(payload: dict) -> None:
    """
    Webhook が Railway など別ホストで動くとき、ローカルの JSON は届かない。
    LINE_PENDING_SYNC_URL があれば同一ペイロードを POST し、返信時に job_id を紐づける。
    """
    url = os.getenv("LINE_PENDING_SYNC_URL", "").strip()
    if not url:
        return
    secret = os.getenv("LINE_PENDING_SYNC_SECRET", "").strip()
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=20)
        if r.status_code != 200:
            print(f"line_pending_sync_http_error status={r.status_code} body={r.text[:500]}")
    except Exception as e:
        print(f"line_pending_sync_failed={e}")


def build_user_friendly_question(selected: dict) -> str:
    target_type = str(selected.get("type", "")).strip()
    target_text = str(selected.get("text", "")).strip()
    quoted = f"「{target_text}」" if target_text else "該当箇所"

    ask_line = {
        "service_candidate": f"{quoted} の正式名称（正しい呼び方）を教えてください。",
        "organization_candidate": f"{quoted} は会社・組織名として正しいですか？正しい正式名称を教えてください。",
        "proper_noun_candidate": f"{quoted} の正しい表記（人名・固有名詞）を教えてください。",
        "suspicious_word": f"{quoted} は誤変換の可能性があります。正しい語句を教えてください。",
    }.get(target_type, f"{quoted} の正しい表現を教えてください。")

    # ユーザー向けは1文中心にし、必要最小限の引用のみ添える
    if target_text:
        return f"{ask_line}\n引用: {quoted}"
    return ask_line


def main() -> None:
    load_dotenv_local()
    parser = argparse.ArgumentParser(
        description=(
            "Task 5 接続スライス: unknown_points から質問を1件生成し、"
            "LINE送信内容を作成（必要時のみ送信）"
        )
    )
    parser.add_argument("--job-id", required=True, help="対象ジョブID")
    parser.add_argument(
        "--input-root",
        default="data/transcriptions",
        help="ジョブディレクトリのルート（デフォルト: data/transcriptions）",
    )
    parser.add_argument(
        "--unknowns",
        default=None,
        help="不明箇所JSON（未指定時: {input_root}/{job_id}/unknown_points.json）",
    )
    parser.add_argument(
        "--text",
        default=None,
        help="文脈抽出用テキスト（未指定時: after_qa -> ai -> merged の順に探索）",
    )
    parser.add_argument(
        "--question-output",
        default=None,
        help="質問生成結果JSON（未指定時: {input_root}/{job_id}/question_result.json）",
    )
    parser.add_argument(
        "--message-output",
        default=None,
        help="LINE送信用メッセージ保存先（未指定時: {input_root}/{job_id}/question_message.txt）",
    )
    parser.add_argument(
        "--send-line",
        action="store_true",
        help="指定時のみ LINE API へ push する（未指定時は送信しない）",
    )
    parser.add_argument(
        "--debug-selection",
        action="store_true",
        help="選定の上位候補（value/impact/risk/dependency 等）を標準出力に出す",
    )
    parser.add_argument(
        "--min-question-value",
        type=int,
        default=8,
        help="この値未満の候補は質問せず完了扱い（デフォルト: 8）",
    )
    args = parser.parse_args()

    job_dir = os.path.join(args.input_root, args.job_id)
    unknowns_path = args.unknowns or os.path.join(job_dir, "unknown_points.json")
    question_output = args.question_output or os.path.join(job_dir, "question_result.json")
    message_output = args.message_output or os.path.join(job_dir, "question_message.txt")
    os.makedirs(os.path.dirname(question_output) or ".", exist_ok=True)

    if not os.path.isfile(unknowns_path):
        raise FileNotFoundError(f"unknowns file not found: {unknowns_path}")
    unknown_points = load_unknown_points(unknowns_path)

    context_text_path = resolve_context_text_path(args.job_id, args.input_root, args.text)
    full_text = ""
    if context_text_path:
        with open(context_text_path, "r", encoding="utf-8") as f:
            full_text = f.read()

    if not unknown_points:
        result_payload = {
            "job_id": args.job_id,
            "question_status": "none",
            "message": "不明箇所は0件のため、質問は生成しません。",
            "selected_unknown": None,
            "question_text": "",
        }
    else:
        if args.debug_selection:
            deduped_dbg, _ = deduplicate_unknown_points_by_type_text(unknown_points)
            print(format_top_candidates_debug(deduped_dbg, full_text), flush=True)
        selected, selection_audit = select_one_unknown_value_based(unknown_points, full_text)
        value = int(selection_audit.get("value", 0))
        if value < args.min_question_value:
            pop_value_fields(selected)
            result_payload = {
                "job_id": args.job_id,
                "question_status": "none",
                "message": (
                    "推測に頼らず読める水準に達しているため、追加質問は行いません。"
                    f"（top_value={value}, threshold={args.min_question_value}）"
                ),
                "selected_unknown": selected,
                "selection_audit": selection_audit,
                "question_text": "",
            }
        else:
            pop_value_fields(selected)
            question_text = build_user_friendly_question(selected)
            question_id = str(uuid.uuid4())
            result_payload = {
                "job_id": args.job_id,
                "question_id": question_id,
                "question_status": "generated",
                "message": "",
                "selected_unknown": selected,
                "selection_audit": selection_audit,
                "question_text": question_text,
            }
            write_line_pending_context(
                job_id=args.job_id,
                question_id=question_id,
                question_text=question_text,
                selected_unknown=selected,
                selection_audit=selection_audit,
            )

    with open(question_output, "w", encoding="utf-8") as f:
        json.dump(result_payload, f, ensure_ascii=False, indent=2)

    message_text = build_line_message(result_payload)
    with open(message_output, "w", encoding="utf-8") as f:
        f.write(message_text)

    line_push = "skipped"
    if args.send_line:
        if result_payload.get("question_status") != "generated":
            line_push = "skipped_no_question"
        else:
            line_user_id = os.getenv("LINE_USER_ID")
            line_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
            if not line_user_id:
                raise RuntimeError("LINE_USER_ID is not set.")
            if not line_token:
                raise RuntimeError("LINE_CHANNEL_ACCESS_TOKEN is not set.")
            push_line_message(
                channel_access_token=line_token,
                user_id=line_user_id,
                text=message_text,
            )
            line_push = "sent"

    print(f"job_id={args.job_id}")
    print(f"unknowns={unknowns_path}")
    print(f"context_text={context_text_path or '(not found)'}")
    print(f"question_result={question_output}")
    print(f"line_message={message_output}")
    print(f"question_status={result_payload.get('question_status')}")
    print(f"line_push={line_push}")


if __name__ == "__main__":
    main()
