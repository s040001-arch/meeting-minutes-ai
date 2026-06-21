"""発言録の分節サマリ見出しを生成する。

長文の発言録を斜め読みできるよう、トピック転換点で分節し
「▼○○○○」形式の見出し(15〜30字の具体的体言止め)を差し込む。
見出しは Markdown の H3 (`### ▼xxx`) として書き出し、Google Docs
export 側で HEADING_3 (太字+見出し階層) スタイルが適用される。

二段構え:
  1) 主経路: Opus 1回呼び出しで「トピック境界の検出」+「具体的見出しの生成」を
     同時に実施。start_phrase 文字列マッチでセクション境界を確定する。
  2) フォールバック: Opus が失敗した場合、機械的な ~1,500 字分割 +
     Sonnet 並列要約に切り替える(従来挙動)。

非致命: ANTHROPIC_API_KEY 未設定や両経路失敗時は原文をそのまま返す。
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
from typing import Any

import anthropic

from anthropic_prompt_cache import OPUS_MODEL_ID, cached_system
from meeting_profile import format_meeting_profile_for_prompt

# --- 機械分割フォールバック用 (粒度を細かく: 改善C) ---
SECTION_TARGET_CHARS = 1500
SECTION_MIN_CHARS = 1000

# --- Sonnet (フォールバックの並列要約用) ---
SUMMARY_MODEL = "claude-sonnet-4-6"
SUMMARY_MAX_TOKENS = 200
SUMMARY_TIMEOUT_SEC = 120
MAX_PARALLEL = 4

# --- Opus (統合分節+要約) ---
INTEGRATED_MODEL = OPUS_MODEL_ID
# 3時間(~60K字)会議で 25-30 セクション × ~150 tokens = ~4500 tokens を想定し余裕を持たせる
INTEGRATED_MAX_TOKENS = 8000
# 3時間の入力で Opus は 3-5 分かかる可能性があるため余裕を持たせる
INTEGRATED_TIMEOUT_SEC = 480
INTEGRATED_MIN_INPUT_CHARS = 1500  # これ未満は見出し無し
INTEGRATED_MIN_SECTIONS = 2  # 最低限の分節数(下回ったらフォールバック)
INTEGRATED_COVERAGE_MIN_RATIO = 0.75  # マッチした境界で本文の何割をカバーすべきか

SUMMARY_PREFIX = "▼"
HEADING_PREFIX = "### "  # Markdown H3, export 側で HEADING_3 にマップ
FALLBACK_HEADING_OPENING = "冒頭の挨拶と会議の導入"  # 最初の境界が本文先頭でない場合の暫定見出し


_PARAGRAPH_SEP = re.compile(r"\n\s*\n+")
_WS_RE = re.compile(r"\s+")


# ============================================================
# 共通ユーティリティ
# ============================================================


def _clean_summary_line(raw: str) -> str:
    """Claude 出力から見出し1行を取り出して整える。"""
    if not raw:
        return ""
    first_line = ""
    for line in raw.splitlines():
        s = line.strip()
        if s:
            first_line = s
            break
    if not first_line:
        return ""
    cleaned = first_line.strip().lstrip("#").strip()
    cleaned = cleaned.strip("「」『』\"'`")
    cleaned = re.sub(r"^[▼▶■◆●・\-\*]+\s*", "", cleaned)
    cleaned = cleaned.rstrip("。．.,、")
    if not cleaned:
        return ""
    if len(cleaned) > 60:
        cleaned = cleaned[:59].rstrip() + "…"
    return cleaned


# ============================================================
# 主経路: Opus 統合分節 + 要約
# ============================================================


def _build_integrated_system_prompt(meeting_profile: dict[str, Any] | None) -> str | list:
    profile_block = format_meeting_profile_for_prompt(meeting_profile or {})
    # Phase 2: Layer 2 由来の世界モデル(関連企業/人物/手法)を inject。
    # 見出しの表記揃え(嘱託再雇用者・エンゲージメントサーベイ 等)に寄与する。
    world_block = ""
    try:
        from world_knowledge_store import get_runtime_knowledge_block
        world_block = get_runtime_knowledge_block(
            meeting_profile=meeting_profile, purpose="correction",
        )
    except Exception as e:  # noqa: BLE001
        print(f"summary_world_knowledge_fetch_failed={e!r}")
    static_prompt = (
        "あなたは議事録の発言録に分節サマリ見出しを差し込む担当です。"
        "渡された発言録全文を読み、議題・話題の自然な切れ目で分節し、"
        "各セクションを表す見出しを生成して返します。"
        "見出しは後で読者(相原)が斜め読みして「ここ詳しく見たい」と判断するための目印です。"
        + "\n\n【分節ルール(改善A)】"
        "\n- 話題が明確に切り替わる発言を境界に取る"
        "\n  例の境界フレーズ: 「次に〜の話なんですけど」「で、もう1つの話」「○○の方ですが」"
        "\n  「ちょっと別の話題なんですが」「では理念浸透の方ですが」など"
        "\n- 1セクション目安: 1,200〜2,000字。議題が変わる場合は短くても切ってよい"
        "\n- 上限: 1セクション 2,500字を超えないこと(壁感を避ける)"
        "\n- セクション数の目安: 1時間の発言録(~20,000字)で 8〜15セクション"
        "\n- 機械的な等間隔分割は禁止。あくまで意味境界で切ること"
        "\n\n【見出し(summary)ルール(改善B)】"
        "\n- 15〜30字の体言止め、名詞句中心"
        "\n- **固有名詞・数字・具体的な決定/提案/論点を1つ以上含めること**"
        "\n  良い例: 「57-59歳ターゲット層への絞り込み案と上司巻き込み」"
        "\n  悪い例: 「対象層の議論」「内容設計について」(抽象すぎる)"
        "\n- 文末に句点・引用符を付けない"
        "\n- 同じ議題名(例: 『シニア研修』『理念浸透』)を見出しの先頭に何度も使わず差別化する"
        "\n- 「〜について」「〜の話」「〜の議論」のような冗長な接尾辞は避ける"
        "\n\n【表記正規化ルール(改善D)】"
        "\n- 上記の【この会議について】に記載の参加者名・専門用語表記に揃えること"
        "\n- 例: 『嘱託再雇用者』『エンゲージメントサーベイ』『理念浸透』など"
        "\n- 発言録本文内の表記揺れ(例: 『再雇用社員』『嘱託社員』が混在)があれば、"
        "  meeting_profile 記載の表記を優先して見出しに使う"
        "\n\n【start_phrase ルール(極めて重要)】"
        "\n- 各セクションの先頭に位置する 30〜60 字を `start_phrase` として返すこと"
        "\n- **入力テキストから一字一句変えずに**抜き出すこと(コピペすること)"
        "\n  - 句読点、改行、半角/全角空白、括弧、フィラー全てそのまま"
        "\n  - 表記揺れの修正、要約、言い換え、省略は禁止"
        "\n- この文字列を後段で `text.find(start_phrase)` で検索しセクション境界を確定する"
        "\n- 最初のセクションの start_phrase は発言録全体の最初の 30〜60 字"
        "\n- 各 start_phrase は入力に1度しか出現しない十分にユニークな文字列にすること"
        "\n  (同じフレーズが他箇所にもあると境界が誤確定する)"
        "\n\n【出力形式】"
        "\n以下の JSON のみを出力。説明文・前置き・コードフェンス禁止。"
        '\n{"sections": ['
        '\n  {"start_phrase": "(入力先頭の30-60字)", "summary": "(15-30字の具体的見出し)"},'
        '\n  {"start_phrase": "(次セクション先頭の30-60字)", "summary": "..."},'
        "\n  ..."
        "\n]}"
    )
    return cached_system(static_prompt, profile_block + world_block)


def _strip_code_fences(raw: str) -> str:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned


def _extract_json_object(raw: str) -> dict[str, Any]:
    cleaned = _strip_code_fences(raw)
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    m = re.search(r"\{[\s\S]*\}", cleaned)
    if not m:
        raise ValueError("no JSON object found in integrated response")
    return json.loads(m.group(0))


def _call_opus_integrated(text: str, meeting_profile: dict[str, Any] | None) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    client = anthropic.Anthropic(api_key=api_key)
    system_prompt = _build_integrated_system_prompt(meeting_profile)
    resp = client.messages.create(
        model=INTEGRATED_MODEL,
        max_tokens=INTEGRATED_MAX_TOKENS,
        timeout=INTEGRATED_TIMEOUT_SEC,
        system=system_prompt,
        messages=[{"role": "user", "content": text}],
    )
    parts: list[str] = []
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", "") == "text":
            parts.append(str(getattr(block, "text", "") or ""))
    return "\n".join(parts)


def _normalize_for_search(s: str) -> str:
    return _WS_RE.sub("", s)


def _map_normalized_idx_to_original(text: str, target_norm_idx: int) -> int:
    """空白除去後のインデックスを元テキストのインデックスに復元する。"""
    if target_norm_idx <= 0:
        return 0
    norm_idx = 0
    for orig_idx, ch in enumerate(text):
        if norm_idx == target_norm_idx:
            return orig_idx
        if not ch.isspace():
            norm_idx += 1
    return len(text)


def _find_start_phrase(full_text: str, phrase: str) -> int:
    """start_phrase を full_text 内で検索しオフセットを返す。見つからない場合 -1。"""
    if not phrase:
        return -1
    idx = full_text.find(phrase)
    if idx >= 0:
        return idx
    # 空白を無視したマッチを試行
    norm_text = _normalize_for_search(full_text)
    norm_phrase = _normalize_for_search(phrase)
    if not norm_phrase:
        return -1
    idx_norm = norm_text.find(norm_phrase)
    if idx_norm < 0:
        return -1
    return _map_normalized_idx_to_original(full_text, idx_norm)


def _parse_integrated_sections(
    raw_response: str, full_text: str
) -> list[tuple[int, str]]:
    """Opus 応答から (start_idx, summary) のリストを返す。

    start_phrase が見つからないものはスキップ。
    """
    obj = _extract_json_object(raw_response)
    raw_sections = obj.get("sections")
    if not isinstance(raw_sections, list):
        raise ValueError("sections field is not a list")

    results: list[tuple[int, str]] = []
    for entry in raw_sections:
        if not isinstance(entry, dict):
            continue
        phrase = str(entry.get("start_phrase") or "").strip()
        summary = _clean_summary_line(str(entry.get("summary") or ""))
        if not phrase or not summary:
            continue
        idx = _find_start_phrase(full_text, phrase)
        if idx < 0:
            print(f"start_phrase not found in transcript: {phrase[:40]!r}")
            continue
        results.append((idx, summary))

    # 位置順にソートし、近接する重複は除去
    results.sort(key=lambda x: x[0])
    deduped: list[tuple[int, str]] = []
    for idx, summary in results:
        if deduped and idx - deduped[-1][0] < 100:
            # 100 字以内の境界は無視(近すぎる)
            continue
        deduped.append((idx, summary))
    return deduped


def _assemble_sections_with_offsets(
    full_text: str, offsets_summaries: list[tuple[int, str]]
) -> str:
    """offsets_summaries: [(start_idx, summary), ...] (ソート済み)。

    本文を offsets で分割し、各セクション先頭に ### ▼summary を差し込む。
    最初の境界が本文冒頭でない場合は、冒頭部分にフォールバック見出しを付ける。
    """
    if not offsets_summaries:
        return full_text.strip() + "\n"

    parts: list[str] = []
    first_idx = offsets_summaries[0][0]
    if first_idx > 100:
        # 冒頭の取り残しを救済
        leading = full_text[:first_idx].strip()
        if leading:
            parts.append(
                f"{HEADING_PREFIX}{SUMMARY_PREFIX}{FALLBACK_HEADING_OPENING}\n\n{leading}"
            )

    for i, (start_idx, summary) in enumerate(offsets_summaries):
        # 安全策: 1セクション目だけは start_idx を 0 に寄せる(<=100字の小さなズレを吸収)
        effective_start = 0 if i == 0 and first_idx <= 100 else start_idx
        end_idx = (
            offsets_summaries[i + 1][0]
            if i + 1 < len(offsets_summaries)
            else len(full_text)
        )
        section_text = full_text[effective_start:end_idx].strip()
        if not section_text:
            continue
        parts.append(f"{HEADING_PREFIX}{SUMMARY_PREFIX}{summary}\n\n{section_text}")

    return "\n\n".join(parts) + "\n"


def _try_integrated_split(
    text: str, meeting_profile: dict[str, Any] | None
) -> str | None:
    """Opus 統合呼び出しで分節+要約を試行する。失敗時 None。"""
    try:
        raw = _call_opus_integrated(text, meeting_profile)
    except Exception as e:
        print(f"integrated_split_call_failed={e!r}")
        return None
    try:
        offsets_summaries = _parse_integrated_sections(raw, text)
    except Exception as e:
        print(f"integrated_split_parse_failed={e!r} raw_head={raw[:200]!r}")
        return None
    if len(offsets_summaries) < INTEGRATED_MIN_SECTIONS:
        print(
            f"integrated_split_too_few_sections={len(offsets_summaries)} "
            f"(min={INTEGRATED_MIN_SECTIONS})"
        )
        return None
    # カバレッジ確認: 最後のセクション開始位置が本文末尾近くまで届いているか
    # 大半をカバーしていない場合、Opus が後半を見落としている可能性が高い
    last_start = offsets_summaries[-1][0]
    coverage = last_start / max(len(text), 1)
    if coverage < INTEGRATED_COVERAGE_MIN_RATIO:
        # 最後の見出しが本文の前半に固まってる = 後半が見出し無しになる
        print(
            f"integrated_split_low_coverage last_start={last_start} "
            f"total={len(text)} coverage={coverage:.2f}"
        )
        return None
    return _assemble_sections_with_offsets(text, offsets_summaries)


# ============================================================
# フォールバック: 機械分割 + Sonnet 並列要約 (従来挙動)
# ============================================================


def split_into_sections(
    text: str,
    target_chars: int = SECTION_TARGET_CHARS,
    min_chars: int = SECTION_MIN_CHARS,
) -> list[str]:
    """発言録を段落境界で ~target_chars ずつ分節する(フォールバック用)。"""
    paragraphs = [p.strip() for p in _PARAGRAPH_SEP.split(text.strip()) if p.strip()]
    if not paragraphs:
        return []

    sections: list[list[str]] = []
    current: list[str] = []
    current_chars = 0
    for p in paragraphs:
        current.append(p)
        current_chars += len(p)
        if current_chars >= target_chars:
            sections.append(current)
            current = []
            current_chars = 0
    if current:
        tail_chars = sum(len(p) for p in current)
        if sections and tail_chars < min_chars // 2:
            sections[-1].extend(current)
        else:
            sections.append(current)
    return ["\n\n".join(sec) for sec in sections]


def _build_fallback_system_prompt(meeting_profile: dict[str, Any] | None) -> str | list:
    profile_block = format_meeting_profile_for_prompt(meeting_profile or {})
    static_prompt = (
        "あなたは議事録の発言録分節サマリ担当です。"
        "渡された発言録の一部(数分間の会話塊)を読み、"
        "この塊で何が話されていたかを15〜30字の見出し1行で返してください。"
        + "\n\n【見出しルール】"
        "\n- 名詞句中心、体言止め推奨"
        "\n- 固有名詞・数字・具体的な決定/提案を1つ以上含める(改善B)"
        "\n  良い例: 「57-59歳ターゲット層への絞り込み案」"
        "\n  悪い例: 「対象層の議論」(抽象すぎる)"
        "\n- meeting_profile 記載の表記に揃える(改善D)"
        "\n  例: 『嘱託再雇用者』『エンゲージメントサーベイ』など"
        "\n- 「〜について」「〜の話」「〜の議論」は避ける"
        "\n- 文末に句点を付けない、引用符を付けない"
        "\n- 出力は見出しテキスト1行のみ。前置き・コードフェンス・説明文を一切付けない"
    )
    return cached_system(static_prompt, profile_block)


def _summarize_one_fallback(
    client: anthropic.Anthropic, section_text: str, system_prompt: str | list
) -> str:
    resp = client.messages.create(
        model=SUMMARY_MODEL,
        max_tokens=SUMMARY_MAX_TOKENS,
        temperature=0,
        timeout=SUMMARY_TIMEOUT_SEC,
        system=system_prompt,
        messages=[{"role": "user", "content": section_text}],
    )
    parts: list[str] = []
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", "") == "text":
            parts.append(str(getattr(block, "text", "") or ""))
    raw = "\n".join(p for p in parts if p)
    return _clean_summary_line(raw)


def _summarize_sections_parallel(
    sections: list[str], meeting_profile: dict[str, Any] | None
) -> list[str]:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return ["" for _ in sections]
    client = anthropic.Anthropic(api_key=api_key)
    system_prompt = _build_fallback_system_prompt(meeting_profile)
    results: list[str] = [""] * len(sections)
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_PARALLEL) as executor:
        future_to_idx = {
            executor.submit(_summarize_one_fallback, client, sec, system_prompt): i
            for i, sec in enumerate(sections)
        }
        for future in concurrent.futures.as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                print(f"fallback_section_summary_failed idx={idx} error={e!r}")
                results[idx] = ""
    return results


def _fallback_mechanical(
    text: str, meeting_profile: dict[str, Any] | None
) -> str:
    sections = split_into_sections(text)
    if len(sections) <= 1:
        return text.strip() + "\n"
    summaries = _summarize_sections_parallel(sections, meeting_profile)
    if all(not s for s in summaries):
        return text.strip() + "\n"
    parts: list[str] = []
    for idx, (sec, summary) in enumerate(zip(sections, summaries), start=1):
        heading = (
            f"{HEADING_PREFIX}{SUMMARY_PREFIX}{summary}"
            if summary
            else f"{HEADING_PREFIX}{SUMMARY_PREFIX}（パート{idx}）"
        )
        parts.append(f"{heading}\n\n{sec}")
    return "\n\n".join(parts) + "\n"


# ============================================================
# 公開エントリポイント
# ============================================================


def add_section_headings(
    text: str,
    meeting_profile: dict[str, Any] | None = None,
) -> str:
    """発言録に分節サマリ見出しを付与した markdown を返す。

    1. 主経路: Opus 1回でトピック境界検出 + 具体的見出し生成 (改善A/B/D)
    2. フォールバック: 機械分割 + Sonnet 並列要約
    3. 全失敗時: 原文をそのまま返す
    """
    text_stripped = text.strip()
    if len(text_stripped) < INTEGRATED_MIN_INPUT_CHARS:
        # 短すぎる場合は見出し無しでそのまま
        return text_stripped + "\n"

    primary_result = _try_integrated_split(text_stripped, meeting_profile)
    if primary_result:
        return primary_result

    print("falling back to mechanical split + parallel sonnet summary")
    return _fallback_mechanical(text_stripped, meeting_profile)


def main() -> int:
    """CLI: 単体実行で手元のテキストに見出しを付ける(デバッグ用)。"""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="発言録テキストに分節サマリ見出しを付与する"
    )
    parser.add_argument("--input", required=True, help="入力テキストファイル")
    parser.add_argument(
        "--output",
        default=None,
        help="出力先(未指定時は標準出力)",
    )
    parser.add_argument(
        "--meeting-profile-json",
        default=None,
        help="meeting_profile.json のパス(任意、サマリ品質向上用)",
    )
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        text = f.read()
    profile = None
    if args.meeting_profile_json and os.path.isfile(args.meeting_profile_json):
        with open(args.meeting_profile_json, "r", encoding="utf-8") as f:
            profile = json.load(f)
    result = add_section_headings(text, profile)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(result)
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
