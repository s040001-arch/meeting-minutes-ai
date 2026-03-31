"""
Docs ハブ用: minutes_structured.md を生成する。

デフォルトは「提出・共有向け」: タイトル・発言録本文・その他テンプレのみ。
確認質問・ユーザー回答・内部メタは含めない（回答は recorrect で本文に織り込む前提）。

--include-internal-workspace 指定時のみ、従来どおり確認ワークスペース＋回答記録を付与する。
"""
import argparse
import json
import os
from typing import Any

from generate_minutes_other_sections import extract_title_and_transcript
from generate_one_question import find_context


def _resolve_transcript_for_context(job_id: str, input_root: str) -> str:
    job_dir = os.path.join(input_root, job_id)
    for name in (
        "merged_transcript_after_qa.txt",
        "merged_transcript_ai.txt",
        "merged_transcript.txt",
    ):
        p = os.path.join(job_dir, name)
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                return f.read()
    return ""


def _load_question_result(job_dir: str) -> dict[str, Any] | None:
    path = os.path.join(job_dir, "question_result.json")
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else None


def _load_latest_answer_for_job(answers_path: str, job_id: str) -> dict[str, Any] | None:
    if not os.path.isfile(answers_path):
        return None
    with open(answers_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        return None
    for r in reversed(data):
        if not isinstance(r, dict):
            continue
        if str(r.get("job_id") or "").strip() == job_id:
            return r
    return None


def _reason_from_selected(selected: dict[str, Any] | None) -> str:
    if not selected:
        return ""
    r = str(selected.get("reason") or "").strip()
    if r:
        return r
    t = str(selected.get("type") or "").strip()
    return f"タイプ: {t}" if t else ""


def _build_internal_workspace_md(
    job_id: str,
    input_root: str,
    answers_json: str,
) -> str:
    job_dir = os.path.join(input_root, job_id)
    full_text = _resolve_transcript_for_context(job_id, input_root)
    qres = _load_question_result(job_dir)
    question_status = str((qres or {}).get("question_status") or "")
    question_text = str((qres or {}).get("question_text") or "").strip()
    selected = (qres or {}).get("selected_unknown")
    if not isinstance(selected, dict):
        selected = None

    target_quote = ""
    if selected:
        target_quote = str(selected.get("text") or "").strip()

    prev_ctx, next_ctx = "", ""
    if full_text.strip() and target_quote:
        prev_ctx, next_ctx = find_context(full_text, target_quote)

    reason = _reason_from_selected(selected)
    audit = (qres or {}).get("selection_audit")
    audit_note = ""
    if isinstance(audit, dict):
        audit_note = (
            f"（優先度参考: type_priority_rank={audit.get('type_priority_rank')}, "
            f"risky_band={audit.get('risky_band')}）"
        )

    answer_rec = _load_latest_answer_for_job(answers_json, job_id)
    answer_block = ""
    transcript_updated_note = ""
    if target_quote and full_text.strip() and target_quote not in full_text:
        transcript_updated_note = (
            "\n（※ 下の **発言録** では、この引用に対応する箇所は再補正済みで、表記は回答に沿って置き換わっています。"
            " 前後文脈が「なし」なのは、再補正後の本文に **この引用文字列そのもの** が残っていないためです。）\n"
        )
    if answer_rec:
        aq = str(answer_rec.get("answer_text") or "").strip()
        qq = str(answer_rec.get("question_text") or "").strip()
        if aq:
            answer_block = (
                "\n### 直近に記録された回答（line_answers.json）\n\n"
                f"- 質問（記録）: {qq or '（なし）'}\n"
                f"- 回答: {aq}\n"
            )
            if not (target_quote and full_text.strip() and target_quote not in full_text):
                answer_block += (
                    "\n※ 再補正後は `python run_docs_hub_e2e.py --after-answer ...` で本文へ反映してください。\n"
                )

    if question_status == "none" or not question_text:
        return (
            "## 確認ワークスペース（現時点最新）\n\n"
            "### 現在の確認対象\n\n"
            "- 不明箇所リストからの追加質問はありません（または未生成）。\n\n"
            "### 今回の質問（1問）\n\n"
            "- （なし）\n\n"
            "### 質問理由\n\n"
            "- （該当なし）\n\n"
            "### 仮説（断定しない）\n\n"
            "- 下記は前後文の抜粋のみ。推測による補完は含めません。\n"
            f"{answer_block}\n"
            "### 次のアクション\n\n"
            f"- 回答を `data/line_answers.json` に追記後、`python run_docs_hub_e2e.py --job-id {job_id} --after-answer` で再補正・Docs 更新。\n\n"
        )
    return (
        "## 確認ワークスペース（現時点最新）\n\n"
        "### 現在の確認対象\n\n"
        f"{target_quote or '（テキスト未設定）'}"
        f"{transcript_updated_note}\n\n"
        "### 前後の文脈（参照用）\n\n"
        f"- 前: {prev_ctx or '（なし）'}\n"
        f"- 後: {next_ctx or '（なし）'}\n\n"
        "### 今回の質問（1問）\n\n"
        f"{question_text}\n\n"
        "### 質問理由\n\n"
        f"{reason or '（理由フィールドなし）'} {audit_note}\n\n"
        "### 仮説（断定しない）\n\n"
        "- 自動では推測補完しません。前後文脈のみ上記に示します。\n"
        f"{answer_block}\n"
        "### 次のアクション\n\n"
        + (
            "- 回答の反映と Docs 更新まで完了している場合は、ここでの追加作業は不要です。\n\n"
            if answer_rec
            and target_quote
            and full_text.strip()
            and target_quote not in full_text
            else (
                "- 回答を `data/line_answers.json` に追記（`job_id` を付与）後、\n"
                f"  `python run_docs_hub_e2e.py --job-id {job_id} --after-answer` を実行すると再補正と Docs 更新が行われます。\n\n"
            )
        )
    )


def build_hub_minutes_md(
    job_id: str,
    input_root: str,
    title_override: str | None,
    answers_json: str,
    *,
    include_internal_workspace: bool = False,
) -> str:
    job_dir = os.path.join(input_root, job_id)
    draft_path = os.path.join(job_dir, "minutes_draft.md")
    if not os.path.isfile(draft_path):
        raise FileNotFoundError(f"minutes_draft.md not found: {draft_path} (run generate_minutes_transcript first)")

    with open(draft_path, "r", encoding="utf-8") as f:
        draft = f.read()
    title, transcript_md = extract_title_and_transcript(draft)
    if title_override:
        title = title_override

    other = (
        "## その他項目\n\n"
        "### 参加者\n"
        "- （未設定）\n\n"
        "### 会議概要\n"
        "- （未設定）\n\n"
        "### 決まったこと\n"
        "- （未設定）\n\n"
        "### 残論点\n"
        "- （未設定）\n\n"
        "### Next Action\n"
        "- （未設定）\n\n"
        "### 発言録ルール（重要）\n"
        "- フィラー削除、明らかな誤字修正、文の区切り整理のみ許可\n"
        "- 意味の補完や推測による書き換え、要約生成は禁止\n"
    )

    if include_internal_workspace:
        workspace = _build_internal_workspace_md(job_id, input_root, answers_json)
        return (
            f"# {title}\n\n"
            f"{workspace}"
            "## 発言録（候補）\n\n"
            f"{transcript_md.strip()}\n\n"
            f"{other}"
        )

    return (
        f"# {title}\n\n"
        "## 発言録\n\n"
        f"{transcript_md.strip()}\n\n"
        f"{other}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Docs ハブ用 minutes_structured.md を生成する")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--input-root", default="data/transcriptions")
    parser.add_argument("--title", default=None)
    parser.add_argument(
        "--answers-json",
        default=os.path.join("data", "line_answers.json"),
        help="--include-internal-workspace 時のみ使用（回答・質問メタの表示用）",
    )
    parser.add_argument(
        "--include-internal-workspace",
        action="store_true",
        help="確認ワークスペース・line_answers 由来のブロックを含める（デバッグ・社内用）",
    )
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    out_path = args.output or os.path.join(
        args.input_root, args.job_id, "minutes_structured.md"
    )
    text = build_hub_minutes_md(
        job_id=args.job_id,
        input_root=args.input_root,
        title_override=args.title,
        answers_json=args.answers_json,
        include_internal_workspace=bool(args.include_internal_workspace),
    )
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)

    print(f"job_id={args.job_id}")
    print(f"output={out_path}")
    print("status=docs_hub_minutes_structured")


if __name__ == "__main__":
    main()
