"""Step 4.5 整合性レビュー(ユーザー設計の Step 4.4): Opusで議事録の違和感を検出する。

- 入力: merged_transcript_ai.txt 全文 + meeting_profile + PIXEL辞書(参考)
- 出力: transcript_anomalies.json
- high+auto_fixable: 自動置換 → auto_corrections.json
- medium (+ high で auto_fix 不可): unknown_points.json 副キュー → LINE 質問
- low: [要確認] タグのみ（質問キューには入れない）
- medium/low: 該当箇所に [要確認] タグ付与

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
from anthropic_prompt_cache import OPUS_MODEL_ID, cached_system
from span_correction import apply_span_corrections_batch, build_span_fields


COHERENCE_REVIEW_MODEL = OPUS_MODEL_ID
# 整合性レビューは全文(~20K字)に対し最大数十件の anomaly を JSON で返すため、
# 出力 token 量が膨らみやすい。Opus は 128K まで対応するので 32K を確保して
# truncation を防ぐ(本番ジョブ job_20260528_050751 で 8K では切り詰められて
# parse 失敗 → 整合性レビュー成果物がゼロになる事象を観測)。
COHERENCE_REVIEW_MAX_TOKENS = 32000
COHERENCE_REVIEW_TIMEOUT_SEC = 900

TRANSCRIPT_ANOMALIES_FILENAME = "transcript_anomalies.json"
AUTO_CORRECTIONS_FILENAME = "auto_corrections.json"
CORRECTION_AUDIT_LOG_FILENAME = "correction_audit_log.json"
UNKNOWN_POINTS_FILENAME = "unknown_points.json"

COHERENCE_TYPE = "coherence_review"
COHERENCE_SOURCE = "coherence_review"


def _load_anthropic_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")
    return key


def _build_system_prompt(meeting_profile: dict | None) -> str | list:
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
    static_prompt = (
        "あなたは議事録（逐語録）の品質管理担当です。"
        "目的は「会議で言ったことを正確な文字起こしとして残す」ことです。"
        "入力は Google Pixel レコーダーの音声認識を AI 補正した日本語議事録です。"
        "人間が読んで違和感を持つ箇所（とくに同音異義語の誤変換）を JSON 配列で挙げてください。"
        + "\n\n【検出方針（Phase 3）】"
        "\n- medium（ユーザー確認・質問対象）は**高精度を最優先**。確信が持てないものは medium にしない。"
        "\n- low（タグのみ・質問しない）は recall 用。不確実・もっともらい・口語的な違和感は low に落とす。"
        "\n- 取りこぼし防止のため low で拾ってよいが、medium は慎重に付ける。"
        "\n\n【検出してはいけないもの（誤変換として扱わない）】"
        "\n- 口語フィラー・相槌・言い淀み・比喩・会話上自然な疑問形"
        "\n  例: 『切れる』『かかってる』『こう力だからな』『できないかな』『任せていこう』"
        "\n- 文脈上ふつうに成立する数値・固有名詞・一般語"
        "\n  例: 会議で実際に言及された『85万円』『58万円』『演習2』『吉田さん』"
        "\n  → positive な誤変換根拠（同音異義・文脈不整合・意味不通）が無ければ出力しない"
        "\n\n【検出の優先度（逐語録精度）】"
        "\n最優先: 文脈上明らかにおかしい語（E/B）。人事・研修・価格・日程の会議で頻出する同音誤変換。"
        "\n例:"
        "\n- 『朝にされる』→ 人員配置文脈では『当てられる』『アサインされる』"
        "\n- 『方の味だ』→ 冒頭文脈では『方針だ』『アジェンダ』"
        "\n- 『両手させていただきます』→ 『終了／完了させていただきます』"
        "\n- 『小さいAさん』→ 階層名『G3/A3』『昇格者』"
        "\n- 『10倍ぐらい』→ 直前の計算文脈では『1.5倍ぐらい』"
        "\n- 『早く若い人』→ 『その若い人』"
        "\n- 『発見とか契約』→ 『派遣とか契約』"
        "\n- 『全部品』→ 会社名・製品名の誤変換の疑い"
        "\n- 『嬉しく1時間』等、意味が通らないフレーズ"
        "\n\n【検出対象カテゴリ】"
        "\nA. 明らかな造語(同一会議内に類似語が既出)"
        "\nB. 文脈と整合しない語・意味不明語"
        "\nC. 文として崩壊している箇所"
        "\nD. 助詞・指示語の誤認識"
        "\nE. 同音異義語の選択ミス"
        "\n\n【検出量】"
        "\n- medium は原則 15 件以下（質問品質優先）。"
        "\n- low は追加で最大 15 件まで（目視用タグ）。合計 30 件上限。"
        "\n\n【既知の Pixel 誤変換パターン(機械補正済み、再検出不要)】"
        f"\n{known_patterns}"
        "\n\n【出力スキーマ】JSON 配列のみを返すこと。説明文・コードフェンス・前置きは一切付けない。"
        "各要素は次のキーを持つ:"
        '\n{"context":"前後10-20字を含む引用（後方互換・短い引用）",'
        '"anomaly_word":"違和感の中心語（後方互換・必須）",'
        '"anomaly_type":"A|B|C|D|E",'
        '"estimated_correction":"推定正解語・短い候補（1〜15字程度。後方互換・質問に提示する語）",'
        '"span_text":"異常を含む最小の文脈スパン（前後含む、20-80字程度）",'
        '"span_corrected":"span_text 全体を修正した後のスパン（全文。estimated_correction を含む）",'
        '"confidence":"high|medium|low",'
        '"auto_fixable":true/false,'
        '"reason":"判定根拠を1文（50字以内）"}'
        "\n\n【確信度の判定基準（厳格化）】"
        "\n- high (auto_fixable=true): 推定正解語が同一テキスト内に既出 かつ "
        "音声認識誤りであることが明白。span_corrected を必ず埋める。"
        "\n- medium: **具体的な単一候補**がある、または文脈不整合が明確で span_corrected が定まる。"
        "ユーザーへの質問対象。**口語・数値のみの違和感は medium にしない**。"
        "\n- low: 候補が複数 / 不確実 / もっともらい / フィラー的 / 固有名詞で確信が持てない"
        "\n\n違和感が無ければ空配列 [] を返してください。"
    )
    return cached_system(static_prompt, profile_block + world_block)


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
    # Opus 4.8 は temperature/assistant prefill を受け付けない。
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
    llm_span_corrected = str(item.get("span_corrected") or "").strip()
    # estimated が空のとき、短い span_corrected のみ候補語として流用（長文スパンは不可）
    if not estimated and llm_span_corrected and len(llm_span_corrected) <= 40:
        estimated = llm_span_corrected
    anomaly_type = str(item.get("anomaly_type") or "B").strip().upper()
    anomaly_type = anomaly_type[:1] if anomaly_type else "B"

    # auto_fixable は LLM 判断より厳格に決定する:
    # - confidence=high かつ word/estimated 共に非空・相異
    # - word が同テキストに既出(置換対象が実在)
    # - estimated またはその語幹が同テキストに既出(造語/誤変換の確証)
    # - 候補が複数(／ や / で区切られている)場合は不可
    # - estimated が短すぎる(1字)場合は誤置換リスクが高いので不可
    has_multi_candidate = "／" in estimated or "/" in estimated
    span_fields = build_span_fields(
        text,
        word=word,
        estimated=estimated or llm_span_corrected,
        hint_pos=pos,
        context=ctx,
        llm_span_text=str(item.get("span_text") or "").strip(),
    )
    span_corrected = llm_span_corrected or str(span_fields.get("span_corrected") or estimated or "").strip()
    span_start = int(span_fields.get("span_start") or -1)
    auto_fixable = (
        confidence == "high"
        and bool(estimated)
        and bool(word)
        and word != estimated
        and span_start >= 0
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
        "context_position_in_transcript": pos if pos >= 0 else span_start,
        "span_text": span_fields.get("span_text") or "",
        "span_start": span_fields.get("span_start", -1),
        "span_end": span_fields.get("span_end", -1),
        "span_corrected": span_corrected,
    }


def _write_correction_audit_log(path: str, entries: list[dict]) -> None:
    """Persist high-confidence auto_fix audit entries (correct/delete only, no keep)."""
    audit_rows: list[dict] = []
    for entry in entries:
        action = str(entry.get("action") or "correct").strip().lower()
        if action not in {"correct", "delete"}:
            continue
        audit_rows.append(
            {
                "anomaly_id": entry.get("anomaly_id"),
                "action": action,
                "before": entry.get("before"),
                "after": entry.get("after"),
                "confidence": entry.get("confidence"),
                "reason": entry.get("reason", ""),
                "span_start": entry.get("span_start"),
                "span_end": entry.get("span_end"),
                "span_text": entry.get("span_text") or "",
                "span_corrected": entry.get("span_corrected") or entry.get("after") or "",
            }
        )
    if not audit_rows:
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump(audit_rows, f, ensure_ascii=False, indent=2)


def _apply_high_auto_fixes(
    text: str,
    anomalies: list[dict],
    auto_log_path: str,
    audit_log_path: str,
) -> tuple[str, list[dict]]:
    fixed, entries = apply_span_corrections_batch(text, anomalies)
    if entries:
        with open(auto_log_path, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
        _write_correction_audit_log(audit_log_path, entries)
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
    """medium + high(non-auto_fix) を unknown_points 副キューへ変換。

    high+auto_fixable は既に置換済みなので含めない。
    **low は質問キューに入れない**（[要確認] タグのみ。目視用）。
    除外条件:
      - anomaly_word が空
      - word が 3 字未満 / 30 字超
      - high なのに推定正解が空
      - confidence=low
    """
    out: list[dict] = []
    for an in anomalies:
        conf = an.get("confidence")
        if conf == "high" and an.get("auto_fixable"):
            continue
        if conf == "low":
            continue
        if conf not in {"high", "medium"}:
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
        if conf == "high" and not str(
            an.get("estimated_correction") or an.get("span_corrected") or ""
        ).strip():
            continue
        span_corrected = str(an.get("span_corrected") or an.get("estimated_correction") or "").strip()
        out.append(
            {
                "type": COHERENCE_TYPE,
                "source": COHERENCE_SOURCE,
                "text": (an.get("context") or word)[:220],
                "context": str(an.get("context") or "").strip()[:220],
                "anomaly_word": word,
                "anomaly_id": an.get("anomaly_id"),
                "estimated_correction": an.get("estimated_correction") or span_corrected,
                "span_text": str(an.get("span_text") or "").strip(),
                "span_corrected": span_corrected,
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
    audit_log_path = os.path.join(job_dir, CORRECTION_AUDIT_LOG_FILENAME)
    fixed_text, auto_entries = _apply_high_auto_fixes(
        text, enriched, auto_log_path, audit_log_path
    )
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
        "correction_audit_log_path": audit_log_path if auto_entries else None,
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
