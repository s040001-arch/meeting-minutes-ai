"""最終批評パス: 完成した整文を単一目的で全文一読し、残存問題を検出・修正する。

背景 (2026-07-09 NREPT案件):
    パイプライン各層は多目的＋ガードレールで保守的に動くため、
    「表記ゆれの不統一」「崩れ断片の残存」「相槌の織り込み」のような
    全体を見れば分かる問題を取りこぼす。チャットでの人手レビューと同じ
    「批評だけ」を最終成果物に対して行う層を追加する。

モード (env FINAL_REVIEW_MODE):
    - "shadow" (デフォルト): 検出のみ。final_review_report.json に記録。本文は変更しない。
    - "apply": 検出に加え、安全条件を満たす high 確信の修正だけ本文に適用。
    - "off": 何もしない（API 呼び出しなし）。

モデル (env FINAL_REVIEW_MODEL): デフォルトは Sonnet（批評は破壊操作ではないため
    安価なモデルから試せる）。必要なら Opus 等に差し替え可能。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import anthropic

from anthropic_prompt_cache import OPUS_MODEL_ID, cached_system

FINAL_REVIEW_REPORT_FILENAME = "final_review_report.json"
DEFAULT_FINAL_REVIEW_MODEL = OPUS_MODEL_ID
FINAL_REVIEW_MAX_TOKENS = 16000
FINAL_REVIEW_TIMEOUT_SEC = 300
FINAL_REVIEW_CHAR_CAP = 60_000
FINAL_REVIEW_MAX_FINDINGS = 60
FINAL_REVIEW_MAX_ROUNDS = 3

# apply モードの安全条件
APPLY_MIN_QUOTE_LEN = 4
APPLY_MAX_FIXES = 50
APPLY_LEN_RATIO_MIN = 0.3
APPLY_LEN_RATIO_MAX = 3.0
# fix が quote に無い内容文字（漢字・かな等）を何種類まで持ち込めるか。
# 同音異義の1字置換（時→次、刺→差）は通し、語の推測追加（「解決研修は」等）は弾く。
APPLY_MAX_NEW_CONTENT_CHARS = 16

_PUNCT_CHARS = set("、。！？!?・…「」『』（）()[]{}〈〉 \u3000\n\t,.")
_NUMBER_TOKEN_RE = re.compile(r"\d+(?:[.,、]\d+)*")


def resolve_final_review_mode() -> str:
    raw = os.environ.get("FINAL_REVIEW_MODE", "").strip().lower()
    if raw in ("off", "shadow", "apply"):
        return raw
    return "shadow"


def resolve_final_review_model() -> str:
    return (
        os.environ.get("FINAL_REVIEW_MODEL", "").strip()
        or DEFAULT_FINAL_REVIEW_MODEL
    )


def _build_system_prompt(
    notation_block: str,
    meeting_profile_block: str = "",
) -> str | list:
    static_prompt = (
        "あなたは完成済みの議事録（要約セクションと発言録・整文）の最終レビュアーです。"
        "唯一の任務は、残存する品質問題を全文一読で発見して報告することです。"
        "本文の書き直しはしません。発見のみを JSON 配列で返してください。"
        "\n\n【全文照合（必須）】"
        "\n- 参加者・議題・決定事項・残論点・Next Action と発言録を相互に照合する。"
        "\n- 要約側が発言録の曖昧な語を勝手に確定、別表記を括弧併記、"
        "または発言録にない情報へ言い換えていれば必ず検出する。"
        "\n- 発言録内でも、同じ人物・制度・研修を指す前後の発言を照合する。"
        "\n\n【検出対象（4種のみ）】"
        "\n1. notation: 同一概念の表記不統一・同音異義の誤選択"
        "（例: 同じ文書内に『7年次』と『8年時』が混在 → 少数派が誤りの疑い。"
        "『にってい→2点』のような音の誤変換も含む）"
        "\n2. fragment: 文として成立していない崩れ断片"
        "（例: 『あんまり見と一緒でフレームアップめっちゃいます』のような意味不明文、"
        "言いかけの残骸『します。』の孤立）"
        "\n3. backchannel: 話し手の文中に縫い込まれた聞き手の相槌"
        "（例: 『はいで、L2が5クラス』『実施をしない方向、はいに考えています』）"
        "\n4. unnatural: 文脈上明らかに不自然な語・表現"
        "（例: 稟議文脈の『決済』、『傘を刺そう』等の表記誤り）"
        "\n\n【検出してはいけないもの】"
        "\n- 口語らしさ・話し言葉のくだけた表現（整文は逐語性を残す方針）"
        "\n- 固有名詞・数値・事実（正誤を判断する材料がなければ触れない）。"
        "ただし、末尾の機械抽出候補、同一文書内の多数表記、質問への回答と復唱、"
        "要約と発言録の不一致は判断材料なので、固有名詞でも必ず検討する"
        "\n- `[要確認]` タグ済みの箇所（既に人の確認待ち）"
        "\n- `▼` 見出し行"
        "\n\n【人名ゆれの扱い（重要）】"
        "\n- 機械抽出された人名ゆれ候補は必ず前後の組織・役割まで照合する。"
        "近接する質問への回答と復唱が同一人物を指し、片方だけ別姓なら、"
        "前後の文脈と文書内の他の表記から正しい姓を判断する。"
        "\n- 例: 『どなたですか？山口という——川口さんか、山口さん』かつ"
        "見出し・要約も『山口部長』なら、挟まった『川口』は聞き取り揺れ。"
        "quote 全体を自然な応答に直した fix を high で提示する。"
        "\n- 正しい姓を一意に決められない場合は medium、fix は空文字とする。"
        "\n- ただし、顧客側の部署・役職として語られる人物と、"
        "『弊社の紹介』『自己紹介』で登場する提供側の参加者は別人である。"
        "一文字違いの姓でも、組織・役割が異なるなら表記ゆれとして報告しない。"
        "\n- 同じ部署の説明内でも、部長になる人物と現担当者など複数人物が"
        "併存し得る。発言が質問・復唱・役割確認として成立するなら、"
        "姓が異なるだけで同一人物と決めつけず報告しない。"
        "\n- 会議プロファイルの参加者名はファイル名等から得た強い根拠である。"
        "近接文脈で同じ役職・人物を指し、一方の姓だけが参加者一覧にあり、"
        "別人を示す根拠がなければ、参加者一覧の姓を正として high の fix を出す。"
        "\n\n【一般語・専門用語の文脈確定】"
        "\n- 『何本部長』のように直後の役職質問から助詞脱落が一意なら"
        "『何の本部長』として high。"
        "\n- 対照群ごとに同じ調査を行い差分をリフト値とする説明で、"
        "『承認にそれぞれ』等の意味不明語があれば、調査方法から"
        "『両群にそれぞれ』と一意に定まるため high。"
        "\n- 承認フローで『○○が上がってくる』の目的語が『応募』になっていれば、"
        "文脈上『稟議』と一意に定まるため high。"
        "\n- 正式名称と、指示対象が明確な省略形（例: 戦略推進室／戦略室）の"
        "併用は自然な話し言葉であり、notation として報告しない。"
        "\n- 『〜するようなこうかな』のように一般語一字の誤認識で"
        "『〜するような形かな』と一意に定まるものは high。"
        "\n- 『今後部長とか〜になる人』のように読点の欠落だけで"
        "意味が一意に回復するものは、読点を補った fix を high。"
        "\n\n【出力スキーマ】JSON 配列のみ。説明・前置き・コードフェンス禁止。"
        "\n各要素:"
        '\n{"type":"notation|fragment|backchannel|unnatural",'
        '"quote":"本文から一字一句そのまま抜いた該当箇所（10〜60字）",'
        '"issue":"何が問題か1文",'
        '"fix":"quote 全体の修正後文字列（確信がなければ空文字）",'
        '"confidence":"high|medium|low"}'
        "\n\n【確信度】"
        "\n- high: 文脈から修正が一意に定まる（fix 必須）。一般語・助詞・単位・"
        "慣用句の明白な音声誤認識は、必ず quote 全体の自然な fix を提示する"
        "\n  例: 『200秒か300秒のKPIツリー』→『200行か300行のKPIツリー』、"
        "『聞く薬を開発』→『効く薬を開発』、『3位いっぱい』→『三位一体』"
        "\n- medium: 問題は確実だが修正候補が複数あり一意に定まらない。"
        "自然な fix が一つだけ提示できるなら medium にせず high とする"
        "\n- low: 違和感レベル"
        f"\n\n問題がなければ空配列 [] を返す。最大{FINAL_REVIEW_MAX_FINDINGS}件。"
    )
    variable_blocks = "\n\n".join(
        x for x in (meeting_profile_block, notation_block) if x.strip()
    )
    return cached_system(static_prompt, variable_blocks)


def _extract_text(resp: Any) -> str:
    parts: list[str] = []
    for block in getattr(resp, "content", None) or []:
        t = getattr(block, "text", None)
        if t:
            parts.append(t)
    return "".join(parts)


def _parse_findings(raw: str) -> list[dict]:
    s = (raw or "").strip()
    start = s.find("[")
    end = s.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(s[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict)]


def _call_reviewer(
    text: str,
    meeting_profile: dict[str, Any] | None = None,
) -> list[dict]:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")
    notation_block = ""
    try:
        from notation_consistency import build_notation_block_for_text

        notation_block = build_notation_block_for_text(text)
    except Exception as e:  # noqa: BLE001
        print(f"final_review_notation_block_failed={e!r}")
    profile_block = ""
    try:
        from meeting_profile import format_meeting_profile_for_prompt

        profile_block = format_meeting_profile_for_prompt(meeting_profile or {})
    except Exception as e:  # noqa: BLE001
        print(f"final_review_profile_block_failed={e!r}")
    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=resolve_final_review_model(),
        max_tokens=FINAL_REVIEW_MAX_TOKENS,
        timeout=FINAL_REVIEW_TIMEOUT_SEC,
        system=_build_system_prompt(notation_block, profile_block),
        messages=[
            {
                "role": "user",
                "content": text[:FINAL_REVIEW_CHAR_CAP],
            }
        ],
    )
    return _parse_findings(_extract_text(resp))


def apply_safe_fixes(text: str, findings: list[dict]) -> tuple[str, list[dict], list[dict]]:
    """high 確信・quote 一意・長さ比が穏当な修正のみ適用する。

    返り値: (適用後テキスト, applied, skipped)
    """
    applied: list[dict] = []
    skipped: list[dict] = []
    out = text
    for f in findings:
        quote = str(f.get("quote") or "")
        fix = str(f.get("fix") or "")
        confidence = str(f.get("confidence") or "").lower()
        new_content_chars = {
            ch for ch in fix if ch not in _PUNCT_CHARS and ch not in quote
        }
        finding_type = str(f.get("type") or "").lower()
        new_char_limit = (
            APPLY_MAX_NEW_CONTENT_CHARS
            if finding_type in {"notation", "unnatural"}
            else 2
        )
        reason = ""
        if confidence != "high" or not fix or fix == quote:
            reason = "not_high_or_no_fix"
        elif len(quote) < APPLY_MIN_QUOTE_LEN:
            reason = "quote_too_short"
        elif "[要確認]" in quote or "[要確認]" in fix:
            reason = "flagged_span"
        elif out.count(quote) != 1:
            reason = f"quote_count={out.count(quote)}"
        elif not (
            APPLY_LEN_RATIO_MIN <= len(fix) / max(len(quote), 1) <= APPLY_LEN_RATIO_MAX
        ):
            reason = "length_ratio_out_of_range"
        elif _NUMBER_TOKEN_RE.findall(quote) != _NUMBER_TOKEN_RE.findall(fix):
            # 数字そのものの変更は文脈推論だけで自動適用しない。
            reason = "numeric_tokens_changed"
        elif len(new_content_chars) > new_char_limit:
            reason = f"too_many_new_chars={len(new_content_chars)}"
        elif len(applied) >= APPLY_MAX_FIXES:
            reason = "max_fixes_reached"
        else:
            candidate = out.replace(quote, fix, 1)
            try:
                from fact_integrity_gate import verify_fact_integrity

                gate = verify_fact_integrity(out, candidate)
                if not gate.ok:
                    reason = f"fact_integrity:{'|'.join(gate.violations)}"
            except Exception as e:  # noqa: BLE001
                reason = f"fact_integrity_error:{type(e).__name__}"
        if reason:
            skipped.append({**f, "skip_reason": reason})
            continue
        out = out.replace(quote, fix, 1)
        applied.append(f)
    return out, applied, skipped


def run_final_review(
    *,
    job_dir: str,
    text: str,
) -> tuple[str, dict[str, Any]]:
    """整文テキストへの最終批評を実行する。

    返り値: (テキスト(applyモードなら修正済み), レポートdict)
    off モードや失敗時は入力テキストをそのまま返す（非致命）。
    """
    mode = resolve_final_review_mode()
    report: dict[str, Any] = {
        "mode": mode,
        "model": resolve_final_review_model(),
        "input_chars": len(text),
        "findings": [],
        "applied": [],
        "skipped": [],
        "rounds": [],
    }
    if mode == "off" or not text.strip():
        return text, report

    meeting_profile: dict[str, Any] = {}
    try:
        from meeting_profile import load_meeting_profile

        meeting_profile = load_meeting_profile(job_dir)
    except Exception as e:  # noqa: BLE001
        print(f"final_review_profile_load_failed={e!r}")

    out_text = text
    for round_no in range(1, FINAL_REVIEW_MAX_ROUNDS + 1):
        try:
            findings = _call_reviewer(out_text, meeting_profile)
        except Exception as e:  # noqa: BLE001
            print(f"final_review_failed round={round_no} error={e!r}")
            report["error"] = repr(e)
            break

        applied: list[dict] = []
        skipped: list[dict] = []
        candidate = out_text
        if mode == "apply":
            candidate, applied, skipped = apply_safe_fixes(out_text, findings)
        report["rounds"].append(
            {
                "round": round_no,
                "input_chars": len(out_text),
                "findings": findings,
                "applied": applied,
                "skipped": skipped,
            }
        )
        report["findings"] = findings
        report["skipped"] = skipped
        report["applied"].extend(applied)
        out_text = candidate
        if mode != "apply" or not applied:
            break
    else:
        # Audit once more after the last applied round.  Never publish on the
        # assumption that the last edit created no new issue.
        try:
            findings = _call_reviewer(out_text, meeting_profile)
            _, _unused, skipped = apply_safe_fixes(out_text, findings)
            report["rounds"].append(
                {
                    "round": FINAL_REVIEW_MAX_ROUNDS + 1,
                    "audit_only": True,
                    "input_chars": len(out_text),
                    "findings": findings,
                    "applied": [],
                    "skipped": skipped,
                }
            )
            report["findings"] = findings
            report["skipped"] = skipped
            report["round_limit_reached"] = bool(findings)
        except Exception as e:  # noqa: BLE001
            report["error"] = repr(e)
    _write_report(job_dir, report)
    print(
        "final_review_done "
        f"mode={mode} findings={len(report['findings'])} "
        f"applied={len(report['applied'])} skipped={len(report['skipped'])}"
    )
    return out_text, report


def _write_report(job_dir: str, report: dict[str, Any]) -> None:
    try:
        path = os.path.join(job_dir, FINAL_REVIEW_REPORT_FILENAME)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"final_review_report_write_failed={e!r}")
