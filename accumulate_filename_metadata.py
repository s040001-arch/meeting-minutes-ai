"""ファイル名メタ情報のナレッジ蓄積モジュール（Step 17b）。

ジョブ完了後、ファイル名から抽出した会議メタ情報
（日付・顧客名・参加者・会議内容）を、将来のジョブで再利用できるよう
Knowledge Sheet へ蓄積する。

これにより、同じ顧客企業や同じ参加者の会議が再度処理されるときに、
Whisper の語彙バイアスや AI 補正で正しく扱えるようになる。

【蓄積対象】
- 顧客企業（外部会議のみ）: 「○○＝顧客企業（会議形態: 営業/提案/etc.）」
- 顧客側の人物（推定）: 「○○さん＝顧客企業○○の担当者」
- プレセナ側の人物（既にナレッジに居る既知の人）: 既存情報を上書きしない
- 顧客との継続案件名: 「○○プロジェクト＝顧客企業○○との継続案件」

【蓄積しない】
- 既にナレッジに存在する用語（重複は Claude マージで除外される）
- 日付（ジョブ固有情報、再利用価値が低い）
"""
from __future__ import annotations

import json
import os
from typing import Any

from ai_correct_text import _append_visible_log
from knowledge_sheet_store import (
    knowledge_store_enabled,
    load_knowledge_memos,
    save_knowledge_memos,
    _load_anthropic_api_key,
)


def _build_qa_pairs_from_metadata(
    parsed_filename: dict[str, Any],
) -> list[dict[str, str]]:
    """ファイル名構造化情報から、ナレッジ蓄積用の擬似 Q&A ペアを生成する。

    既存の merge_all_answers_into_knowledge_store と同じインタフェースで
    渡せるよう、{"question_text": ..., "answer_text": ...} 形式で返す。
    """
    pairs: list[dict[str, str]] = []
    scope = parsed_filename.get("meeting_scope")
    customer = parsed_filename.get("customer")
    attendees = parsed_filename.get("attendees") or []
    topics = parsed_filename.get("topics") or []

    if scope == "external" and customer:
        pairs.append({
            "question_text": f"「{customer}」とは何か（議事録ファイル名から自動抽出）",
            "answer_text": (
                f"{customer}＝顧客企業の1社。"
                "プレセナ・ストラテジック・パートナーズが研修・コンサルティング等を提供する取引先。"
                "音声認識で誤変換されやすい固有名詞のため、表記の統一が重要。"
            ),
        })

    if scope == "external" and customer and attendees:
        attendees_str = "、".join(str(a) for a in attendees)
        pairs.append({
            "question_text": f"{customer}の会議に参加している人物（議事録ファイル名から自動抽出）",
            "answer_text": (
                f"{attendees_str}は{customer}との会議の参加者として記録されている。"
                "プレセナ社員と顧客側担当者が混在する可能性があるが、"
                "苗字のみの場合は同一姓の人物と同一視しないよう注意。"
            ),
        })

    if scope == "internal" and attendees:
        attendees_str = "、".join(str(a) for a in attendees)
        pairs.append({
            "question_text": f"社内会議の参加者（議事録ファイル名から自動抽出）",
            "answer_text": (
                f"{attendees_str}はプレセナ・ストラテジック・パートナーズ内の社内会議の参加者。"
            ),
        })

    if scope == "external" and customer and topics:
        topics_str = "、".join(str(t) for t in topics)
        pairs.append({
            "question_text": f"{customer}との会議の議題・案件名（議事録ファイル名から自動抽出）",
            "answer_text": (
                f"「{topics_str}」は{customer}との会議で扱われた議題・案件。"
                "継続案件名の可能性があり、後続会議でも同じ用語が再登場することがある。"
            ),
        })

    return pairs


def _merge_filename_metadata_with_claude(
    existing_memos: list[str],
    qa_pairs: list[dict[str, str]],
    *,
    parsed_filename: dict[str, Any] | None = None,
    meeting_profile: dict[str, Any] | None = None,
) -> dict:
    """ファイル名由来のメタ情報を、既存ナレッジと統合する。

    既存の merge_all_answers_into_knowledge_store とほぼ同じ振る舞いだが、
    プロンプトを「ファイル名由来のメタ情報」専用に調整する。
    """
    import anthropic

    client = anthropic.Anthropic(api_key=_load_anthropic_api_key())
    profile = meeting_profile or {}
    customer = str(profile.get("customer_name") or parsed_filename.get("customer") or "").strip()
    topic = str(profile.get("topic") or "").strip()
    if not topic and parsed_filename.get("topics"):
        topic = "、".join(str(t) for t in parsed_filename.get("topics") or [])
    profile_hint = ""
    if customer or topic:
        profile_hint = (
            f"\n\n【今回の会議コンテキスト】顧客: {customer or '不明'} / 議題: {topic or '不明'}"
        )
    system_prompt = (
        "あなたは議事録AIの再利用ナレッジ管理アシスタントです。"
        "入力として、既存のナレッジメモ一覧と、議事録ファイル名から自動抽出された"
        "会議メタ情報（顧客企業・参加者・案件名）の質問・回答ペアが与えられます。"
        "目的は、各情報にジョブ横断で再利用価値があるかを判断し、"
        "既存ナレッジと重複・類似があれば統合整理したうえで、更新後のナレッジ一覧全体を返すことです。"
        + profile_hint
        + "\n\n【ルール】"
        "\n- correction_dict のような置換辞書は作らず、1行1件の自由記述メモだけを管理する"
        "\n- 既存メモに同じ顧客企業や同じ参加者の情報があれば、合成して情報量を増やす（追記）"
        "\n- 例: 既存「川口さん＝NRE物流事業部」+ 新規「川口は野村不動産の会議参加者」"
        "\n        → 「川口さん（かわぐち）＝野村不動産 NRE物流事業部所属、複数会議に参加」"
        "\n- 既に登録済みの『プレセナ側の人物』には顧客所属を勝手に上書きしない（誤情報注入防止）"
        "\n- 一般的すぎる情報（『顧客企業の1社』だけのような薄い情報）は追加しない"
        "\n- 顧客企業のフルネーム、参加者の苗字、案件・サービス名は再利用価値が高いので積極的に統合"
        "\n- 出力は JSON オブジェクトのみ"
        '\n- 形式: {"updated_knowledge":["..."],"action":"unchanged|updated","reason":"string"}'
    )
    payload = {
        "existing_knowledge": existing_memos,
        "qa_pairs": qa_pairs,
        "source": "filename_metadata_auto_extracted",
    }
    resp = client.messages.create(
        model="claude-sonnet-4-20250514",
        # 既存 knowledge memos 全体を返させるため、累積数が増えると出力も大きくなる。
        # 4000 では本番(memos 155件)で truncation 観測のため引き上げ。
        max_tokens=16000,
        temperature=0,
        system=system_prompt,
        messages=[
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            {"role": "assistant", "content": "{"},
        ],
    )
    texts = []
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", "") == "text":
            texts.append(str(getattr(block, "text", "") or ""))
    raw_text = "\n".join(t for t in texts if t).strip()
    # 共有の寛容な抽出器を使用（プレフィル "{" の補完・コードフェンス除去対応）
    from knowledge_sheet_store import _extract_json_object
    parsed = _extract_json_object(raw_text)
    updated_raw = parsed.get("updated_knowledge", [])
    if not isinstance(updated_raw, list):
        updated_raw = []
    # normalize
    seen: set[str] = set()
    updated: list[str] = []
    for item in updated_raw:
        text = " ".join(str(item or "").strip().split())
        if not text or text in seen:
            continue
        seen.add(text)
        updated.append(text)
    if not updated and existing_memos:
        updated = list(existing_memos)
    return {
        "updated_knowledge": updated,
        "action": str(parsed.get("action") or "").strip() or "unchanged",
        "reason": str(parsed.get("reason") or "").strip(),
    }


def accumulate_filename_metadata(
    *,
    parsed_filename: dict[str, Any] | None,
    visible_log_path: str | None = None,
    meeting_profile: dict[str, Any] | None = None,
) -> dict:
    """ファイル名メタ情報を Knowledge Sheet に反映する。

    失敗時もパイプラインを止めない（結果は dict で返す）。
    """
    if not parsed_filename:
        _append_visible_log(visible_log_path, "  ファイル名メタ情報なし → 蓄積をスキップ")
        return {"skipped": True, "reason": "no_parsed_filename", "enabled": True, "updated": False}

    qa_pairs = _build_qa_pairs_from_metadata(parsed_filename)
    if not qa_pairs:
        _append_visible_log(
            visible_log_path,
            "  ファイル名から蓄積対象のメタ情報を抽出できませんでした",
        )
        return {"skipped": True, "reason": "no_qa_pairs", "enabled": True, "updated": False}

    if not knowledge_store_enabled():
        _append_visible_log(
            visible_log_path,
            "  ファイル名メタ情報蓄積: スキップ（KNOWLEDGE_SHEET_IDが未設定）",
        )
        return {"enabled": False, "updated": False, "reason": "knowledge_sheet_id_missing"}

    _append_visible_log(
        visible_log_path,
        f"  ファイル名メタ情報をナレッジに反映中...（{len(qa_pairs)}件の情報源）",
    )

    try:
        existing = load_knowledge_memos()
    except Exception as e:
        _append_visible_log(
            visible_log_path,
            f"  既存ナレッジの読み込みでエラー: {e!r}",
        )
        return {"error": str(e), "enabled": True, "updated": False}

    try:
        merged = _merge_filename_metadata_with_claude(
            existing,
            qa_pairs,
            parsed_filename=parsed_filename,
            meeting_profile=meeting_profile,
        )
    except Exception as e:
        _append_visible_log(
            visible_log_path,
            f"  ファイル名メタ情報蓄積でエラー: {e!r}",
        )
        return {"error": str(e), "enabled": True, "updated": False}

    updated = merged.get("updated_knowledge") or []
    changed = updated != existing
    if changed:
        try:
            save_knowledge_memos(updated)
        except Exception as e:
            _append_visible_log(
                visible_log_path,
                f"  ナレッジ保存でエラー: {e!r}",
            )
            return {"error": str(e), "enabled": True, "updated": False}

    result = {
        "enabled": True,
        "updated": changed,
        "reason": merged.get("reason", ""),
        "action": merged.get("action", "unchanged"),
        "knowledge_count_before": len(existing),
        "knowledge_count_after": len(updated),
        "qa_pairs_count": len(qa_pairs),
    }

    if changed:
        before = result["knowledge_count_before"]
        after = result["knowledge_count_after"]
        diff = after - before
        _append_visible_log(
            visible_log_path,
            f"  ファイル名メタ情報蓄積が完了しました（{before}件 → {after}件、差分{diff:+d}件）",
        )
    else:
        reason = result["reason"] or "新規情報なし"
        _append_visible_log(
            visible_log_path,
            f"  ファイル名メタ情報蓄積: 変更なし（{reason}）",
        )

    return result
