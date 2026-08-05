#!/usr/bin/env python3
"""correction_dict.json の全エントリを LLM で再分類し、盲目置換を安全化する。

背景 (2026-08-05 ユーザー優先事項):
    correction_dict.json は機械補正で全ジョブに無条件適用される。
    従来の scope 判定は「3文字以下 or 既知リスト」だけだったため、
    「根本さん」「本店にあります」「小さいやつ」のような実在語・一般的
    言い回しが盲目置換辞書に残り、別クライアントの本文を汚染しうる。

処理:
    1. バックアップ作成
    2. 各エントリを decide_scope（ヒューリスティック + LLM）で判定
       - context（実在語の可能性あり）→ dict から削除し、学習辞書に
         scope=context で記録（文脈判断つきヒントとして継続利用）
       - global（実在しえない崩れ表記）→ dict に残す
    3. 結果レポートを data/knowledge/ に保存

Railway 上（cwd=/app）で実行する。
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

REPO = Path.cwd()
sys.path.insert(0, str(REPO))

from learned_corrections_store import (  # noqa: E402
    add_learned_correction,
    suggest_scope,
)

# --- 自己完結の scope 判定（Railway 上の旧モジュールでも動くようインライン） ---
import re  # noqa: E402

_NAME_LIKE_RE = re.compile(r"^[\u4E00-\u9FFF]{1,3}(さん|様|氏|くん|ちゃん)$")


def _classify_scope_with_llm(wrong: str, right: str) -> str | None:
    import os

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic

        client = anthropic.Anthropic()
        resp = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=200,
            timeout=30,
            system=(
                "あなたは日本語の音声認識誤変換ペアの安全性を判定します。"
                "与えられた「誤」の文字列が、誤変換ではなく正当な日本語"
                "（実在の人名・地名・一般語・自然な言い回し）として、"
                "別の会議の書き起こしに出現しうるかを判定してください。"
                '出力はJSONのみ: {"plausible_real": true|false, "reason": "短い理由"}'
            ),
            messages=[{"role": "user", "content": f"誤: {wrong}\n正: {right}"}],
        )
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        )
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        verdict = json.loads(m.group(0))
        return "context" if bool(verdict.get("plausible_real")) else "global"
    except Exception as exc:  # noqa: BLE001
        print(f"scope_llm_classify_failed={exc!r}")
        return None


def decide_scope(wrong: str, right: str = "") -> str:
    w = (wrong or "").strip()
    if suggest_scope(w) == "context":
        return "context"
    if _NAME_LIKE_RE.match(w):
        return "context"
    llm = _classify_scope_with_llm(w, (right or "").strip())
    if llm in ("context", "global"):
        return llm
    return "context"


# --- ここまでインライン判定 ---

DICT_PATH = REPO / "data" / "correction_dict.json"
REPORT_PATH = REPO / "data" / "knowledge" / "correction_dict_migration_report.json"


def main() -> int:
    if not DICT_PATH.is_file():
        print(f"correction_dict_missing={DICT_PATH}")
        return 0

    data = json.loads(DICT_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        print("correction_dict_not_a_dict")
        return 1

    backup = DICT_PATH.with_name(
        f"correction_dict.backup_{date.today().strftime('%Y%m%d')}_llm.json"
    )
    if not backup.exists():
        shutil.copyfile(DICT_PATH, backup)
        print(f"backup={backup.name}")

    kept: dict[str, str] = {}
    report: list[dict[str, str]] = []
    migrated = 0
    removed_only = 0
    for wrong, right in data.items():
        w = str(wrong).strip()
        r = str(right).strip()
        if not w or not r:
            continue
        scope = decide_scope(w, r)
        if scope == "global":
            kept[w] = r
            report.append({"wrong": w, "right": r, "verdict": "keep_global"})
            continue
        res = add_learned_correction(
            wrong=w,
            right=r,
            via="dict_migration_llm",
            job_id="",
            confidence="high",
            scope="context",
        )
        action = res.get("action")
        if action in ("added", "updated"):
            migrated += 1
            verdict = "moved_to_context"
        else:
            # 学習辞書に載せられなくても、盲目置換の危険は同じなので外す
            removed_only += 1
            verdict = f"removed_only({res.get('reason')})"
        report.append({"wrong": w, "right": r, "verdict": verdict})
        print(f"{verdict}: {w!r} -> {r!r}")

    DICT_PATH.write_text(
        json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(
            {
                "migrated_at": datetime.now().isoformat(timespec="seconds"),
                "total": len(data),
                "kept_global": len(kept),
                "moved_to_context": migrated,
                "removed_only": removed_only,
                "entries": report,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        f"done total={len(data)} kept_global={len(kept)} "
        f"moved_to_context={migrated} removed_only={removed_only}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
