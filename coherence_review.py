"""Step 4.5 整合性レビュー(ユーザー設計の Step 4.4): Opusで議事録の違和感を検出する。

- 入力: merged_transcript_ai.txt 全文 + meeting_profile + PIXEL辞書(参考)
- 出力: transcript_anomalies.json
- high+auto_fixable: 自動置換 → auto_corrections.json
- medium: unknown_points.json に副キュー (source=coherence_review) として追加
- low/medium: 該当箇所に [要確認] タグ付与

Step 4.3 のチャンクごと補正では拾えない違和感(造語/意味不明語/破綻箇所)を、
全文一括で Opus に読ませて検出する。失敗してもパイプラインは止めない設計。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import anthropic

from mechanical_correct_text import PIXEL_RECOGNIZER_REPLACEMENTS
from meeting_profile import format_meeting_profile_for_prompt, load_meeting_profile
from pipeline_build import get_pipeline_build_info
from recognition_batch import find_standalone_word, is_valid_coherence_question_word


COHERENCE_REVIEW_MODEL = "claude-opus-4-7"
# 整合性レビューは全文(~20K字)に対し最大数十件の anomaly を JSON で返すため、
# 出力 token 量が膨らみやすい。Opus は 128K まで対応するので 32K を確保して
# truncation を防ぐ(本番ジョブ job_20260528_050751 で 8K では切り詰められて
# parse 失敗 → 整合性レビュー成果物がゼロになる事象を観測)。
COHERENCE_REVIEW_MAX_TOKENS = 32000
COHERENCE_REVIEW_TIMEOUT_SEC = 900

TRANSCRIPT_ANOMALIES_FILENAME = "transcript_anomalies.json"
AUTO_CORRECTIONS_FILENAME = "auto_corrections.json"
UNKNOWN_POINTS_FILENAME = "unknown_points.json"

COHERENCE_TYPE = "coherence_review"
COHERENCE_SOURCE = "coherence_review"


def _load_anthropic_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")
    return key


def _build_system_prompt(meeting_profile: dict | None) -> str:
    profile_block = format_meeting_profile_for_prompt(meeting_profile or {})
    # Phase 2: Layer 2 由来の世界モデルを inject (関連企業/人物/手法/相原氏のスタイル)
    world_block = ""
    try:
        from world_knowledge_store import get_runtime_knowledge_block
        world_block = get_runtime_knowledge_block(
            meeting_profile=meeting_profile, purpose="coherence",
        )
    except Exception as e:  # noqa: BLE001
        print(f"coherence_world_knowledge_fetch_failed={e!r}")
    known_patterns = ", ".join(sorted(PIXEL_RECOGNIZER_REPLACEMENTS.keys())[:30])
    return (
        "あなたは議事録（逐語録）の品質管理担当です。"
        "目的は「会議で言ったことを正確な文字起こしとして残す」ことです。"
        "入力は Google Pixel レコーダーの音声認識を AI 補正した日本語議事録です。"
        "人間が読んで違和感を持つ箇所（とくに同音異義語の誤変換）を JSON 配列で挙げてください。"
        + profile_block
        + world_block
        + "\n\n【検出の優先度（逐語録精度）】"
        "\n最優先: 文脈上明らかにおかしい語（E/B）。人事・研修・価格・日程の会議で頻出する同音誤変換を積極的に拾う。"
        "\n例:"
        "\n- 『泳ぐ』『泳いで』→ 引き継ぎ文脈では『任せ』『任せて』"
        "\n- 『学部』→ 人材の文脈では『若い』"
        "\n- 『演習さん』→ 『演習2』『演習二』"
        "\n- 『ベッド』→ 事前課題の文脈では『ベース』"
        "\n- 『全部品』→ 会社名・製品名の誤変換の疑い"
        "\n- 『ネストは奥』→ 『ネストは置く』『ネストは後回し』"
        "\n- 『小さいA』→ 階層名『昇格者』『G3』等"
        "\n- 『嬉しく1時間』等、意味が通らないフレーズ全体"
        "\n- 『新交代』→ 『失礼します』『お待ちください』"
        "\n\n【検出対象カテゴリ】"
        "\nA. 明らかな造語(同一会議内に類似語が既出)"
        "\nB. 文脈と整合しない語・意味不明語（上記例を含む）"
        "\nC. 文として崩壊している箇所"
        "\nD. 助詞・指示語の誤認識"
        "\nE. 同音異義語の選択ミス（A/B と同等に積極的に検出）"
        "\n\n【検出量】"
        "\n- 長文（8,000字超）では、見つかった違和感を最大30件まで列挙する（取りこぼしより過検出を優先）。"
        "\n- 1件も見逃さないよう、同音誤変換を中心に丁寧に走査する。"
        "\n\n【既知の Pixel 誤変換パターン(機械補正済み、再検出不要)】"
        f"\n{known_patterns}"
        "\n\n【出力スキーマ】JSON 配列のみを返すこと。説明文・コードフェンス・前置きは一切付けない。"
        "各要素は次のキーを持つ:"
        '\n{"context":"前後10-20字を含む引用",'
        '"anomaly_word":"違和感のある語",'
        '"anomaly_type":"A|B|C|D|E",'
        '"estimated_correction":"推定正解語(文脈から最もありそうな1つ。無ければ空文字)",'
        '"confidence":"high|medium|low",'
        '"auto_fixable":true/false,'
        '"reason":"判定根拠を簡潔に"}'
        "\n\n【確信度の判定基準】"
        "\n- high (auto_fixable=true): 推定正解語が同一テキスト内に既出 かつ "
        "音声認識誤りであることが明白"
        "\n- medium: 文脈から推定正解が1つに絞れる（同テキスト未出でもよい）。"
        "ユーザー確認用の候補として必ず列挙する"
        "\n- low: 候補が複数 / 固有名詞で確信が持てない / 推定不能"
        "\n\n違和感が無ければ空配列 [] を返してください。"
    )


def _extract_text_from_anthropic(resp) -> str:
    parts: list[str] = []
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", "") == "text":
            parts.append(str(getattr(block, "text", "") or ""))
    return "\n".join(p for p in parts if p).strip()


def _recover_complete_objects_from_truncated_array(text: str) -> list[dict]:
    """JSON 配列が max_tokens で途中切れした場合でも、完了済みの {...} を回収する。

    `[ {a},{b},{c... `のような truncated 入力から、完全に閉じている {a},{b} の
    2件を取り出して返す。文字列内の `{}` も中括弧深度として計上されないよう
    シンプルな state machine で走査する。
    """
    s = (text or "").lstrip()
    if not s or s[0] != "[":
        return []
    out: list[dict] = []
    depth = 0
    in_string = False
    escape = False
    item_start = -1
    i = 1  # skip leading '['
    n = len(s)
    while i < n:
        ch = s[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                if depth == 0:
                    item_start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and item_start >= 0:
                    candidate = s[item_start: i + 1]
                    try:
                        obj = json.loads(candidate)
                        if isinstance(obj, dict):
                            out.append(obj)
                    except json.JSONDecodeError:
                        pass
                    item_start = -1
            elif ch == "]" and depth == 0:
                break
        i += 1
    return out


def _parse_json_array(raw: str) -> list[dict]:
    """寛容な JSON 配列抽出: コードフェンス除去・truncation 耐性付き。

    1. 直接 parse 成功なら返す。
    2. 最大の [...] ブロックを試す。
    3. 途中切れ(`]` が現れない、または末尾の `}` が不完全)の場合は、
       state machine で完了済み top-level objects だけを取り出す。
    """
    s = (raw or "").strip()
    if s.startswith("```"):
        nl = s.find("\n")
        s = s[nl + 1:] if nl >= 0 else s
    if s.endswith("```"):
        s = s[:-3].strip()
    if not s or s in {"[]", "[ ]"}:
        return []
    # 1. 直接 parse
    try:
        loaded = json.loads(s)
        if isinstance(loaded, list):
            return [x for x in loaded if isinstance(x, dict)]
    except json.JSONDecodeError:
        pass
    # 2. 最大の [...] ブロックを抽出
    start = s.find("[")
    end = s.rfind("]")
    if start >= 0 and end > start:
        try:
            loaded = json.loads(s[start: end + 1])
            if isinstance(loaded, list):
                return [x for x in loaded if isinstance(x, dict)]
        except json.JSONDecodeError:
            pass
    # 3. truncation リカバリ: 完了済み {...} だけ回収する
    if start >= 0:
        recovered = _recover_complete_objects_from_truncated_array(s[start:])
        if recovered:
            return recovered
    raise RuntimeError(f"coherence anomalies JSON parse failed: head={s[:200]!r}")


def _call_opus_for_anomalies(text: str, meeting_profile: dict | None) -> list[dict]:
    client = anthropic.Anthropic(api_key=_load_anthropic_api_key())
    system_prompt = _build_system_prompt(meeting_profile)
    # 全文渡し。安全のため60k字を上限(本案件は約2万字なので余裕)
    payload_text = text if len(text) <= 60_000 else text[:60_000]
    # claude-opus-4-7 は temperature/assistant prefill を受け付けない (deprecated)。
    # system prompt で「JSON 配列のみ」を強く要求し、robust parser でフェンス等を除去する。
    resp = client.messages.create(
        model=COHERENCE_REVIEW_MODEL,
        max_tokens=COHERENCE_REVIEW_MAX_TOKENS,
        timeout=COHERENCE_REVIEW_TIMEOUT_SEC,
        system=system_prompt,
        messages=[
            {"role": "user", "content": payload_text},
        ],
    )
    raw = _extract_text_from_anthropic(resp)
    return _parse_json_array(raw)


# 〜化、〜性、〜的など、AI造語で付与されやすい接尾辞。stem 一致判定に使う。
_STEM_SUFFIXES = ("化", "性", "的", "論", "感", "観", "者", "型", "風", "派", "系")


def _is_estimated_known_in_text(estimated: str, text: str) -> bool:
    """推定正解語(または接尾辞を除いた語幹)が同テキスト内に既出かを判定する。

    LLM が「自分語化 → 自分ごと化」のように『〜化』付き造語を提案するとき、
    厳密に『自分ごと化』完全一致を求めると false になる(本文には『自分ごと』のみ)。
    そこで語幹(『自分ごと』)が存在すれば既出扱いとする。
    """
    if not estimated or not text:
        return False
    if estimated in text:
        return True
    if len(estimated) >= 2 and estimated[-1] in _STEM_SUFFIXES:
        stem = estimated[:-1]
        if stem and stem in text:
            return True
    return False


def _enrich_anomaly(item: dict, idx: int, text: str) -> dict:
    word = str(item.get("anomaly_word") or "").strip()
    ctx = str(item.get("context") or "").strip()
    pos = -1
    if word:
        hint = -1
        if ctx:
            hint = text.find(ctx[:20])
        pos = find_standalone_word(text, word, hint_pos=hint)
    if pos < 0 and ctx:
        pos = text.find(ctx[:20])
    confidence = str(item.get("confidence") or "low").strip().lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"
    estimated = str(item.get("estimated_correction") or "").strip()
    anomaly_type = str(item.get("anomaly_type") or "B").strip().upper()
    anomaly_type = anomaly_type[:1] if anomaly_type else "B"

    # auto_fixable は LLM 判断より厳格に決定する:
    # - confidence=high かつ word/estimated 共に非空・相異
    # - word が同テキストに既出(置換対象が実在)
    # - estimated またはその語幹が同テキストに既出(造語/誤変換の確証)
    # - 候補が複数(／ や / で区切られている)場合は不可
    # - estimated が短すぎる(1字)場合は誤置換リスクが高いので不可
    has_multi_candidate = "／" in estimated or "/" in estimated
    auto_fixable = (
        confidence == "high"
        and bool(estimated)
        and bool(word)
        and word != estimated
        and word in text
        and len(estimated) >= 2
        and not has_multi_candidate
        and _is_estimated_known_in_text(estimated, text)
    )
    return {
        "anomaly_id": f"ta_{idx:03d}",
        "context": ctx,
        "anomaly_word": word,
        "anomaly_type": anomaly_type,
        "estimated_correction": estimated,
        "confidence": confidence,
        "auto_fixable": auto_fixable,
        "reason": str(item.get("reason") or "").strip(),
        "context_position_in_transcript": pos,
    }


def _apply_high_auto_fixes(
    text: str, anomalies: list[dict], log_path: str
) -> tuple[str, list[dict]]:
    fixed = text
    entries: list[dict] = []
    for an in anomalies:
        if not an.get("auto_fixable"):
            continue
        wrong = an.get("anomaly_word") or ""
        right = an.get("estimated_correction") or ""
        if not wrong or not right or wrong == right or wrong not in fixed:
            continue
        before_count = fixed.count(wrong)
        fixed = fixed.replace(wrong, right)
        entries.append(
            {
                "anomaly_id": an["anomaly_id"],
                "before": wrong,
                "after": right,
                "occurrences_replaced": before_count,
                "confidence": an.get("confidence"),
                "reason": an.get("reason", ""),
            }
        )
    if entries:
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
    return fixed, entries


def _apply_review_tags(text: str, anomalies: list[dict]) -> str:
    """medium/low の anomaly_word 最初の出現に [要確認] タグを付与する。"""
    out = text
    seen: set[str] = set()
    for an in anomalies:
        if an.get("confidence") not in {"medium", "low"}:
            continue
        word = (an.get("anomaly_word") or "").strip()
        if not word or word in seen:
            continue
        seen.add(word)
        hint = an.get("context_position_in_transcript", -1)
        try:
            hint_pos = int(hint) if hint is not None else -1
        except (TypeError, ValueError):
            hint_pos = -1
        idx = find_standalone_word(out, word, hint_pos=hint_pos)
        if idx < 0:
            continue
        # 既に [要確認] が付いている場合は重複付与しない
        tail_check_pos = idx + len(word)
        if out[tail_check_pos: tail_check_pos + 6] == "[要確認]":
            continue
        out = out[:idx] + word + "[要確認]" + out[idx + len(word):]
    return out


def _coherence_to_unknown_points(anomalies: list[dict]) -> list[dict]:
    """high/medium/low 確信度を unknown_points 形式に変換(FIFO 副キュー扱い)。

    high+auto_fixable は既に置換済みなので含めない。
    それ以外(high で auto_fixable=false / medium / low)は質問対象に含める。
    low も含めることで、本文に [要確認] タグが付くだけで放置されていた
    固有名詞・数値の音声認識ゆれを、相原氏に確認できるようにする。
    ただし以下は LINE 質問として無意味なため除外する:
      - anomaly_word が空
      - word が長すぎる(30字超 = 文崩壊レベルの断片。単語確認の質問に不向き)
      - high なのに推定正解が空(『よいしょ!』等の口語の false-positive)
    """
    out: list[dict] = []
    for an in anomalies:
        conf = an.get("confidence")
        if conf == "high" and an.get("auto_fixable"):
            continue
        if conf not in {"high", "medium", "low"}:
            continue
        word = (an.get("anomaly_word") or "").strip()
        if not word:
            continue
        if not is_valid_coherence_question_word(word):
            continue
        # 30字超の "wordとして長すぎる断片" は単語確認質問に不向き(文崩壊扱い)
        if len(word) > 30:
            continue
        # high なのに推定正解が空なものは false-positive の可能性が高い(『よいしょ!』等の口語)
        if conf == "high" and not str(an.get("estimated_correction") or "").strip():
            continue
        out.append(
            {
                "type": COHERENCE_TYPE,
                "source": COHERENCE_SOURCE,
                "text": (an.get("context") or word)[:220],
                "context": str(an.get("context") or "").strip()[:220],
                "anomaly_word": word,
                "anomaly_id": an.get("anomaly_id"),
                "estimated_correction": an.get("estimated_correction") or "",
                "confidence": conf,
                "reason": an.get("reason", ""),
                "anomaly_type": an.get("anomaly_type", "B"),
                "context_position_in_transcript": an.get(
                    "context_position_in_transcript", -1
                ),
                "status": "open",
            }
        )
    return out


def _merge_into_unknown_points(job_dir: str, coherence_points: list[dict]) -> int:
    """unknown_points.json に副キューを追記する(重複は anomaly_id でガード)。"""
    if not coherence_points:
        return 0
    path = os.path.join(job_dir, UNKNOWN_POINTS_FILENAME)
    existing: list[dict] = []
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                existing = [x for x in data if isinstance(x, dict)]
        except (OSError, json.JSONDecodeError):
            existing = []
    existing_ids = {
        str(it.get("anomaly_id") or "") for it in existing if it.get("anomaly_id")
    }
    added = 0
    for p in coherence_points:
        if str(p.get("anomaly_id") or "") in existing_ids:
            continue
        existing.append(p)
        added += 1
    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    return added


def run_coherence_review(job_dir: str) -> dict:
    ai_path = os.path.join(job_dir, "merged_transcript_ai.txt")
    if not os.path.isfile(ai_path):
        raise FileNotFoundError(f"ai transcript not found: {ai_path}")
    with open(ai_path, "r", encoding="utf-8") as f:
        text = f.read()

    profile = load_meeting_profile(job_dir)
    build_info = get_pipeline_build_info()

    anomalies_raw = _call_opus_for_anomalies(text, profile)
    enriched = [_enrich_anomaly(a, i + 1, text) for i, a in enumerate(anomalies_raw)]

    anomalies_path = os.path.join(job_dir, TRANSCRIPT_ANOMALIES_FILENAME)
    with open(anomalies_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "job_id": os.path.basename(job_dir),
                "model": COHERENCE_REVIEW_MODEL,
                "pipeline_correction_version": build_info[
                    "pipeline_correction_version"
                ],
                "git_commit": build_info["git_commit"],
                "input_chars": len(text),
                "anomalies": enriched,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    auto_log_path = os.path.join(job_dir, AUTO_CORRECTIONS_FILENAME)
    fixed_text, auto_entries = _apply_high_auto_fixes(text, enriched, auto_log_path)
    tagged_text = _apply_review_tags(fixed_text, enriched)
    if tagged_text != text:
        with open(ai_path, "w", encoding="utf-8") as f:
            f.write(tagged_text)

    # Phase 1 学習: auto_fix された誤認識ペアを学習辞書に永続化(横断再発防止)。
    # 学習失敗は本ステップ全体を止めない(非致命)。
    learned_added = _persist_auto_fixes_to_learned_dict(
        auto_entries=auto_entries,
        anomalies=enriched,
        job_id=os.path.basename(job_dir),
    )

    coherence_points = _coherence_to_unknown_points(enriched)
    added_to_unknowns = _merge_into_unknown_points(job_dir, coherence_points)

    return {
        "anomalies_total": len(enriched),
        "high_count": sum(1 for a in enriched if a.get("confidence") == "high"),
        "medium_count": sum(1 for a in enriched if a.get("confidence") == "medium"),
        "low_count": sum(1 for a in enriched if a.get("confidence") == "low"),
        "auto_fixed_count": len(auto_entries),
        "coherence_question_candidates": len(coherence_points),
        "added_to_unknown_points": added_to_unknowns,
        "learned_dict_added": learned_added,
        "anomalies_path": anomalies_path,
        "auto_corrections_path": auto_log_path if auto_entries else None,
    }


def _persist_auto_fixes_to_learned_dict(
    *, auto_entries: list[dict], anomalies: list[dict], job_id: str
) -> int:
    """auto_fix された (before, after) ペアを learned_corrections.json に追加する。

    anomalies から例示用の context を引っ張ってきて、学習エントリに保存する。
    返り値: 新規追加 + 既存更新の件数(skipped は含まない)。
    """
    if not auto_entries:
        return 0
    try:
        from learned_corrections_store import add_learned_correction
    except Exception as e:  # noqa: BLE001
        print(f"learned_corrections_import_failed={e!r}")
        return 0

    # anomaly_id -> context のマップ(例示用)
    ctx_by_id: dict[str, str] = {}
    for an in anomalies:
        aid = str(an.get("anomaly_id") or "").strip()
        if aid:
            ctx_by_id[aid] = str(an.get("context") or "").strip()

    persisted = 0
    for entry in auto_entries:
        wrong = str(entry.get("before") or "").strip()
        right = str(entry.get("after") or "").strip()
        if not wrong or not right:
            continue
        aid = str(entry.get("anomaly_id") or "").strip()
        example = ctx_by_id.get(aid, "")
        try:
            result = add_learned_correction(
                wrong=wrong,
                right=right,
                via="coherence_review",
                job_id=job_id,
                example=example,
                confidence=str(entry.get("confidence") or "high"),
            )
        except Exception as e:  # noqa: BLE001
            print(f"learned_corrections_add_failed wrong={wrong!r} err={e!r}")
            continue
        if result.get("action") in ("added", "updated"):
            persisted += 1
        else:
            print(
                f"learned_corrections_skipped wrong={wrong!r} right={right!r} "
                f"reason={result.get('reason')}"
            )
    return persisted


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Step 4.5 整合性レビュー (Opus): 議事録の違和感を検出"
    )
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--input-root", default="data/transcriptions")
    args = parser.parse_args()
    job_dir = os.path.join(args.input_root, args.job_id)
    if not os.path.isdir(job_dir):
        print(f"job_dir not found: {job_dir}", file=sys.stderr)
        return 1
    result = run_coherence_review(job_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
