"""文書内の表記ゆれ（同音異義の不統一）候補を機械抽出する。

背景 (2026-07-09 NREPT案件):
    同じ文書内に「7年次」と「8年時」が混在していたが、coherence 検出は
    「文書全体の表記一貫性照合」という観点を持たず取りこぼした。
    LLM に「探せ」と頼むより、機械で全文突き合わせした候補ペアを渡して
    「この候補の正誤を文脈判定せよ」に変換する方が確実。

仕組み:
    1. 漢字・数字の連なりをトークン抽出（数字列は # に正規化）
    2. 各漢字を同音グループの代表字に写像した「読みスケルトン」を作る
    3. スケルトンが同じで表層が異なるトークン群 = 表記ゆれ候補
    例: 7年次 → #年次 → #年[ジ] / 8年時 → #年時 → #年[ジ] → 衝突 → 候補

出力はあくまで候補。正誤の判定は coherence / 編集者 LLM が文脈で行う。
"""
from __future__ import annotations

import re

# 音声認識で混同されやすい同音（同読み）漢字グループ。
# 網羅ではなく「ビジネス会議で頻出する混同ペア」に絞る（過剰だと候補がノイズ化する）。
_HOMOPHONE_GROUPS: list[str] = [
    "次時事字辞",      # ジ: 年次/年時, 記事/記字
    "期機器基規記",    # キ: 期間/機関/基幹
    "間感観勘管官館完慣環関監",  # カン
    "工講構行効項高交更",        # コウ
    "社車者舎",        # シャ: 社内/車内
    "済裁際再最採催",  # サイ: 決済/決裁
    "性制製正精成生整",  # セイ
    "課科化価可家過",  # カ
    "修収週終州習",    # シュウ
    "士氏師誌資支指",  # シ
    "新真進深振信親",  # シン
    "転天点店展",      # テン
    "回改会解開界階介",  # カイ
    "見件権検研険験健",  # ケン
    "立律率",          # リツ: 自立/自律
    "以意異移委位医",  # イ
    "層想創総送相双",  # ソウ
    "数須州",          # スウ/ス
    "個戸固己",        # コ
]

_CANON: dict[str, str] = {}
for _group in _HOMOPHONE_GROUPS:
    _rep = _group[0]
    for _ch in _group:
        _CANON[_ch] = _rep

_TOKEN_RE = re.compile(r"[0-9０-９一-鿿々]+")
_DIGIT_RUN_RE = re.compile(r"[0-9０-９]+")

MAX_GROUPS = 8
MAX_TOKEN_LEN = 8
CONTEXT_RADIUS = 22

# --- 人名ゆれ候補スキャン (2026-07 THR案件) ---------------------------------
# 背景: 同一人物の姓が『山口という』『川口さんか』のように近音の別姓で混在した
# まま最終議事録に残り、残論点に『山口（川口）部長』という併記が創作された。
# 同音写像（読みスケルトン）では 山/川 のような非同音の聞き取り揺れを拾えない
# ため、敬称・紹介文脈に隣接する人名らしきトークン同士を「同長・1字違い」で
# 突き合わせる専用スキャンを設ける。判定は coherence / 編集者 LLM が文脈で行う。
_NAME_HONORIFIC_PAT = r"さん|様|氏|君|先生|部長|課長|社長|次長|室長|本部長"
_NAME_NAMING_PAT = r"という|っていう|と申します|と言います"
_NAME_TOKEN_RE = re.compile(
    rf"([一-鿿]{{2,3}})(?=(?:{_NAME_HONORIFIC_PAT}|{_NAME_NAMING_PAT}))"
)

MAX_NAME_GROUPS = 4


def _normalize_token(token: str) -> str:
    """数字列を # に畳む（7年次/8年次 を同一表層として扱うため）。"""
    return _DIGIT_RUN_RE.sub("#", token)


def _skeleton(normalized: str) -> str:
    return "".join(_CANON.get(ch, ch) for ch in normalized)


def scan_notation_inconsistencies(text: str) -> list[dict]:
    """全文から表記ゆれ候補グループを抽出する。

    返り値: [{"variants": [{"surface","count","example"}, ...]}, ...]
    variants は出現数の多い順（先頭が多数派 = 正の可能性が高い）。
    """
    if not text:
        return []

    # normalized surface -> {count, first_pos, example_context}
    stats: dict[str, dict] = {}
    for m in _TOKEN_RE.finditer(text):
        token = m.group(0)
        normalized = _normalize_token(token)
        # 漢字を最低2字含むトークンのみ（1字は同音衝突が多すぎてノイズ）
        kanji_count = sum(1 for ch in normalized if ch not in "#")
        if kanji_count < 2 or len(normalized) > MAX_TOKEN_LEN:
            continue
        entry = stats.get(normalized)
        if entry is None:
            lo = max(0, m.start() - CONTEXT_RADIUS)
            hi = min(len(text), m.end() + CONTEXT_RADIUS)
            stats[normalized] = {
                "count": 1,
                "example": text[lo:hi].replace("\n", " "),
            }
        else:
            entry["count"] += 1

    by_skeleton: dict[str, list[str]] = {}
    for normalized in stats:
        by_skeleton.setdefault(_skeleton(normalized), []).append(normalized)

    groups: list[dict] = []
    for skeleton, surfaces in by_skeleton.items():
        if len(surfaces) < 2:
            continue
        variants = sorted(
            (
                {
                    "surface": s,
                    "count": stats[s]["count"],
                    "example": stats[s]["example"],
                }
                for s in surfaces
            ),
            key=lambda v: -v["count"],
        )
        groups.append({"variants": variants})

    # 少数派の出現が少ない（=誤変換らしい）ものを優先
    groups.sort(key=lambda g: (g["variants"][-1]["count"], -g["variants"][0]["count"]))
    return groups[:MAX_GROUPS]


def _one_kanji_diff(a: str, b: str) -> bool:
    """同じ長さで、ちょうど1文字だけ異なるか（山口/川口 型の判定）。"""
    if len(a) != len(b) or a == b:
        return False
    return sum(1 for x, y in zip(a, b) if x != y) == 1


def scan_person_name_variants(text: str) -> list[dict]:
    """敬称・紹介文脈に隣接する人名らしきトークンから、1字違いの姓ペアを抽出する。

    例: 『山口という』x1 と『川口さんか』x1 → 同一人物の聞き取り揺れ候補。
    別人（加藤さんと佐藤さんが両方参加等）の可能性もあるため、あくまで候補。
    正誤・同一人物か否かの判定は coherence / 編集者 LLM が文脈で行う。
    """
    if not text:
        return []

    stats: dict[str, dict] = {}
    for m in _NAME_TOKEN_RE.finditer(text):
        token = m.group(1)
        # 3字取りの取り過ぎ対策: 先頭が助詞的でない漢字連結の一部でも、
        # 2字姓 + 敬称の組み合わせを別途カウントするため末尾2字も登録する
        candidates = {token}
        if len(token) == 3:
            candidates.add(token[-2:])
        for name in candidates:
            entry = stats.get(name)
            if entry is None:
                lo = max(0, m.start() - CONTEXT_RADIUS)
                hi = min(len(text), m.end() + CONTEXT_RADIUS)
                stats[name] = {
                    "count": 1,
                    "example": text[lo:hi].replace("\n", " "),
                }
            else:
                entry["count"] += 1

    names = sorted(stats)
    groups: list[dict] = []
    used: set[str] = set()
    for i, a in enumerate(names):
        if a in used:
            continue
        cluster = [a]
        for b in names[i + 1:]:
            if b not in used and _one_kanji_diff(a, b):
                cluster.append(b)
        if len(cluster) < 2:
            continue
        used.update(cluster)
        variants = sorted(
            (
                {
                    "surface": n,
                    "count": stats[n]["count"],
                    "example": stats[n]["example"],
                }
                for n in cluster
            ),
            key=lambda v: -v["count"],
        )
        groups.append({"variants": variants})

    # 少数派の出現が少ない（=聞き取り揺れらしい）ものを優先
    groups.sort(key=lambda g: (g["variants"][-1]["count"], -g["variants"][0]["count"]))
    return groups[:MAX_NAME_GROUPS]


def format_person_name_block(groups: list[dict]) -> str:
    """人名ゆれ候補を LLM プロンプト注入用のブロック文字列にする。"""
    if not groups:
        return ""
    lines = [
        "\n\n【人名ゆれ候補（機械抽出・全文照合済み）】",
        "以下は同一人物の姓が別表記で混在している可能性がある組（敬称・紹介文脈に隣接する語）。"
        "文脈から同一人物を指すと判断できる場合は、人名の聞き取り揺れ（カテゴリF相当）として"
        "検出し、どちらが正しいか文脈で確定できなければ自動修正せずユーザー確認対象とすること。"
        "明らかに別人（両方が参加者・別々の文脈で登場）なら検出しないこと。",
    ]
    for g in groups:
        parts = [
            f"『{v['surface']}』x{v['count']}（例: …{v['example']}…）"
            for v in g["variants"]
        ]
        lines.append("- " + " ⇔ ".join(parts))
    return "\n".join(lines)


def format_notation_block(groups: list[dict]) -> str:
    """検出候補を LLM プロンプト注入用のブロック文字列にする。"""
    if not groups:
        return ""
    lines = [
        "\n\n【表記ゆれ候補（機械抽出・全文照合済み）】",
        "以下は同音異義の表記不統一の可能性がある組。各組について文脈から正しい表記を判定し、"
        "誤っている側を検出対象（カテゴリE・estimated_correction=文脈に整合する側の表記）とせよ。"
        "出現数の多寡ではなく必ず文脈で正誤を判定すること。"
        "両方とも文脈上正当（別概念）なら検出しないこと。"
        "表層の # は数字を表す（例: #年次 は 7年次・8年次 等）。",
    ]
    for g in groups:
        parts = [
            f"『{v['surface']}』x{v['count']}（例: …{v['example']}…）"
            for v in g["variants"]
        ]
        lines.append("- " + " ⇔ ".join(parts))
    return "\n".join(lines)


def build_notation_block_for_text(text: str) -> str:
    """テキストからスキャン→プロンプトブロック生成までの一括ヘルパー。

    同音表記ゆれ候補と人名ゆれ候補の両ブロックを連結して返す
    （coherence / contextual_editor / final_review の全注入点で共通利用）。
    """
    parts: list[str] = []
    try:
        parts.append(format_notation_block(scan_notation_inconsistencies(text)))
    except Exception as e:  # noqa: BLE001
        print(f"notation_scan_failed={e!r}")
    try:
        parts.append(format_person_name_block(scan_person_name_variants(text)))
    except Exception as e:  # noqa: BLE001
        print(f"person_name_scan_failed={e!r}")
    return "".join(p for p in parts if p)
