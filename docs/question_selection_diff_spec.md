# 質問選定：現行ロジック vs 質問価値ベース（差分仕様）

本文書は **検証・設計用** のみである。記載の「現行」はリポジトリのコードに基づく事実。「新ロジック」は **未実装の仕様案** であり、挙動を保証するものではない。

---

## 1. 現行ロジック整理（事実）

### 1.1 入力

- **選定の直接入力**: `unknown_points.json`（JSON 配列。各要素は少なくとも `type`・`text`・`reason` を持つ dict）。
- **配列の生成**（`run_job_once.py`）:
  1. `extract_unknown_points.py` が `merged_transcript_ai.txt` を文単位ルールで走査し、ヒット文を **先頭から** 配列に追加する。
  2. 生成された `unknown_points.json` をそのまま step 5 の入力に渡す。

### 1.2 処理フロー（E2E step 5）

1. `run_question_cycle_once.py` が `unknown_points.json` を `load_unknown_points` で読み込む。
2. `select_one_unknown_prioritized(unknown_points)` が **1件** を返す。
3. `build_user_friendly_question(selected)` が LINE 向け短文を生成。
4. 結果を `question_result.json`・`question_message.txt` 等に保存（コード上の事実は前回整理のとおり）。

### 1.3 選定ロジック（キー）

実装箇所: `run_question_cycle_once.select_one_unknown_prioritized`。

- `enumerate(unknown_points)` に対し **`min(..., key=key_func)`** で1件を選ぶ。
- キーは **`(risky_band, base_priority, idx)`**（タプル辞書順で最小）。
  - **`risky_band`**: `item["type"]` が `RISKY_TYPE_PRIORITY`（`proper_noun_candidate` / `organization_candidate` / `service_candidate` / `suspicious_word` / `suspicious_number_or_role`）のキーに一致すれば **0**、それ以外（例: `extract_unknown_points` 由来の `固有名詞` / `数値` / `主語`）は **1**。  
    → 現行パイプラインでは主に `extract_unknown_points` 由来の候補が入るため、通常は **1** 側で比較される。`risky_band` は旧拡張との互換のためロジック上は残っている。
  - **`base_priority`**: `RISKY_TYPE_PRIORITY` で `固有名詞` または `数値` にマップしたあと、`TYPE_PRIORITY`（`generate_one_question.py`）で数値化。**固有名詞=0、数値=1、主語=2、マップなし=999**。
  - **`idx`**: 配列インデックス。**上記2つが同じなら小さい方**（先に出現した要素）。

**LLM は「1件を選ぶ」判断には使われない**。現行では候補列挙も `extract_unknown_points.py` のルール処理が中心である。

### 1.4 別経路（事実）

- `generate_one_question.choose_one_unknown` は **`risky_band` を持たない** `min`（`(TYPE_PRIORITY[type], idx)`）。パイプラインの step 5 は **`run_question_cycle_once` のみ**を呼ぶ（`run_job_once.py` より）。

### 1.5 問題点（コードベースで説明）

- **意味内容・質問価値**: 候補間で比較する指標が **ない**（`type` の粗い段階と出現順のみ）。
- **全文との関係**: 選定時に `merged_transcript_ai.txt` を読んでも **`find_context` は呼ばれない**（`resolve_context_text_path` で読む `full_text` は後続で未使用）。
- **配列順依存**: `extract_unknown_points.py` の **出力順** がそのまま **タイブレーク** に効く。
- **二重の選定実装**: `choose_one_unknown` と `select_one_unknown_prioritized` で **優先ルールが一致しない**（単体スクリプト利用時にズレうる）。

---

## 2. 新ロジック（質問価値ベース）の仕様案

以下は **「どう変えるべきか」案** であり、現行コードには存在しない。

### 2.1 全文影響度（impact）

| 項目 | 内容 |
|------|------|
| **定義** | その不明点を解消すると、議事録の解釈が **どれだけ広く変わるか**。 |
| **計算** | **未実装案**: ルール（キーワード・エンティティ種別）と **LLM スコアリング**の併用が現実的。例: 組織名・サービス名・数値の誤りは高 impact、比喩内の1語は低め、等をプロンプトまたはルールで与える。 |
| **必要な入力** | 候補1件ごとの `text` / `type` / `reason`、および **全文**（`merged_transcript_ai.txt` 相当）。 |

### 2.2 波及範囲（coverage）

| 項目 | 内容 |
|------|------|
| **定義** | 1回の回答で **同種の誤りを複数箇所に一括反映できるか**（表記ゆれ・略称統一など）。 |
| **計算** | **未実装案**: 全文に対する同一表記・類似表記の出現回数・スパン（ルール or LLM）。 |
| **必要な入力** | 全文、候補の `text`、正規化後のキー（例: 大文字小文字無視）。 |

### 2.3 会議中心性（centrality）

| 項目 | 内容 |
|------|------|
| **定義** | 当該不明点が **会議の目的・決定・合意** にどれだけ近いか。 |
| **計算** | **未実装案**: セクション推定、見出しキーワード、または LLM に「この会議の主題からの近さ」を点数化させる。 |
| **必要な入力** | 全文、（あれば）アジェンダ・タイトル・ファイル名 stem。 |

### 2.4 代替可能性（recoverability）

| 項目 | 内容 |
|------|------|
| **定義** | **文脈をぼかしたまま** 議事録として成立させられるか（読者が誤解しないか）。 |
| **計算** | **未実装案**: 比喩・フィラー・例示の中の語は recoverability **高**（聞かなくてよい）、契約・金額・固有名は **低**、等をルール or LLM。 |
| **必要な入力** | 候補の文位置、前後文（文脈ウィンドウ）、`type`。 |

### 2.5 確定困難性（non-recoverability）

| 項目 | 内容 |
|------|------|
| **定義** | **AI が推測で補うと危険**か（ハルシネーション・誤固有名の固定化）。 |
| **計算** | **未実装案**: 候補の `reason` や `type` が既に「低確信」を示す場合は高め、等。LLM で「推測禁止領域か」をフラグ化。 |
| **必要な入力** | 候補 dict 全文、既存の `type` / `reason`、ポリシー（spec の禁止事項）。 |

### 2.6 統合（案）

- 各次元を **0–1 または整数スコア** にし、**重み付き和またはレキシコグラフィック順** で最大の1件を選ぶ。重みは **設定ファイル** または **定数** で管理する想定。

---

## 3. 現行との違い（差分）

| 観点 | 現行 | 新ロジック（案） |
|------|------|------------------|
| **選定キー** | `(risky_band, base_priority, idx)` のみ | impact / coverage / centrality / recoverability / non-recoverability（またはその合成スコア） |
| **データ構造** | `unknown_points.json` は配列 of dict（`type`/`text`/`reason`）。監査は `question_result.selection_audit` に **現行キー** | 各候補に **スコア列** または別スキーマ `candidate_scores.json` 等（案） |
| **処理フロー** | マージ済み配列 → 即 `min` | 全文読込 →（任意）特徴抽出 → スコアリング → 最大1件 |
| **LLMの関与** | 現行の選定段階では未使用 | **案**: 選定段階でもスコアリング or ランキングに利用可能 |

---

## 4. 実装変更ポイント（まだ実装しない）

- **`run_question_cycle_once.py`**: `select_one_unknown_prioritized` を **置換**するか、**前段でスコア付与**した配列を渡すか。
- **`generate_one_question.py`**: 単体 CLI 利用時に `choose_one_unknown` と **同じ選定**に揃えるなら、共通モジュールへ **抽出**。
- **新規モジュール案**: `score_question_candidates.py` 等 — 入力 `unknown_points.json` + 全文パス、出力 **スコア付きリスト** または **選ばれた1件**。
- **スコア計算の挿入位置**: `unknown_points.json` 生成の **後**、`run_question_cycle_once` の **選定直前**（`run_job_once` のフローは変えず、関数差し替えで済ませやすい）。
- **設定**: 重み・閾値は `repo_env` / 新yaml / argparse 等（未決）。

---

## 5. 具体例で比較（必須）

**仮定する2候補**（実データの列挙ではなく、仕様説明用の対比）:

- **A**: 文脈上、音声認識で崩れた **比喩内の語**（例: 「隣刑」など、意味が通らない1語）。
- **B**: 同じ会議で **組織・サービス略称が混在**する表記（例: **DHR / THR / PHR** のような、商談の相手・サービス名の理解に直結する揺れ）。

### 5.1 現行でどうなるか（事実に基づく一般論）

- 両方とも `run_question_cycle_once.py` 上で固有名詞系タイプとして扱われれば **同じ `risky_band`** と **`TYPE_PRIORITY` 上は同格（固有名詞系）** になりうる。
- その場合 **先に `unknown_points.json` に並んでいる方**（`idx` が小さい方）が選ばれる。
- **「隣刑」と「THR」のどちらが重要か」は比較されない**。

### 5.2 新ロジック（案）で後者が選ばれうる理由（設計論）

以下は **仕様意図** の説明であり、コードの出力ではない。

- **impact / coverage**: DHR/THR/PHR の整理は、会議全体の **相手・サービス** の解釈に触れ、同表記の **複数箇所** を一度の定義で揃えられる可能性が高い。
- **centrality**: 打ち合わせの主題（仕事力サーベイ × THR 等）に **直結**しやすい。
- **recoverability**: 「隣刑」は **比喩の1語**として読み飛ばしやすく、確定しなくても文脈の「勢い」は残りうる（recoverability **高** → 質問優先度 **低め** にできる）。
- **non-recoverability**: 略称の誤認は **誤った前提** を議事録に残しうる（人間確認の価値が高い）。

**結論（案）**: 価値ベースでは **B（略称・組織の揺れ）を先に聞く**設計にしやすい。現行は **並び順のみ** で A が先なら A が選ばれる。

---

## 6. 自己チェック

- **コードとズレていないか**: セクション1・3・4の「現行」は `run_question_cycle_once` / `run_job_once` / `generate_one_question` / `merge` の記述に対応。
- **推測で書いていないか**: セクション2・5.2 は **仕様案・設計論** と明記した。
- **差分が明確か**: セクション3の表とセクション5で対比。
