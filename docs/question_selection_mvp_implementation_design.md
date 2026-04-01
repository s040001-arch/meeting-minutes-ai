# 質問選定 MVP：impact + recoverability 実装設計

本文書は **実装手順用** の詳細設計である。`docs/question_selection_diff_spec.md` の方針に従う。**コードは本文書作成時点では未変更**。

---

## 1. 実装対象

### 1.1 変更対象ファイル

| ファイル | 変更内容 |
|----------|----------|
| `run_question_cycle_once.py` | 選定呼び出しの差し替え、`full_text` を選定関数へ渡す、`selection_audit` の拡張 |
| `generate_one_question.py` | （任意・第2段）単体 CLI 利用時に **同一選定**へ揃える場合のみ `choose_one_unknown` を共通関数へ委譲 |

### 1.2 変更対象関数

| 関数 | 所在 | 扱い |
|------|------|------|
| `select_one_unknown_prioritized` | `run_question_cycle_once.py` | **置換**する。現行の `(risky_band, base_priority, idx)` 計算は **タイブレーク専用**として内部で再利用するか、小さなヘルパーに分割する。 |
| `main`（`run_question_cycle_once`） | 同上 | `unknown_points` 読込後、既に読んでいる `full_text` を **必ず** 新選定関数に渡す（現状 L184–188 で読み込み済み）。 |

### 1.3 新規追加が必要な関数（推奨モジュール分割）

| 推奨配置 | 関数名 | 責務 |
|----------|--------|------|
| 新規 `question_value_selection.py`（ファイル名は実装時に決定可） | `compute_impact(candidate: dict, full_text: str) -> int` | impact スコア（§2） |
| 同上 | `compute_recoverability(candidate: dict, full_text: str) -> int` | recoverability スコア（§3） |
| 同上 | `compute_tiebreak_key(idx: int, item: dict) -> tuple[int, int, int]` | 現行と同じ `(risky_band, base_priority, idx)`。`TYPE_PRIORITY` / `RISKY_TYPE_PRIORITY` は **`generate_one_question` から import** して重複定義しない |
| 同上 | `select_one_unknown_value_based(unknown_points: list[dict], full_text: str) -> tuple[dict, dict]` | §4 の最終選定。戻り値は現行と同じ `(selected, selection_audit)` |

### 1.4 差し込み位置（既存フロー）

1. `run_job_once.py` は `run_question_cycle_once.py` を呼ぶだけなので **変更不要**（選定は `run_question_cycle_once.main` 内のみ）。
2. `run_question_cycle_once.main` の **L199** 付近  
   `selected, selection_audit = select_one_unknown_prioritized(unknown_points)`  
   を  
   `selected, selection_audit = select_one_unknown_value_based(unknown_points, full_text)`  
   に置き換える（`full_text` は空でも可だが、その場合は §2 の出現回数ボーナスは 0 とするルールを固定する）。

---

## 2. impact の詳細設計

### 2.1 入力

| 入力 | 必須 | 説明 |
|------|------|------|
| `candidate["type"]` | 必須 | `str`。現行パイプラインでは主に `extract_unknown_points` 由来。`run_question_cycle_once.py` には旧拡張互換の type も残っている |
| `candidate["text"]` | 必須 | 引用・検索に使う本文断片 |
| `full_text` | 必須 | `resolve_context_text_path` で得たファイルの全文。空文字のときは §2.5 の出現回数ボーナスは **0** |

`reason` は MVP の impact には **使わない**（将来の non-recoverability 用に残す）。

### 2.2 判定ルール（ベーススコア：整数）

`type_raw = str(candidate.get("type", ""))` に対し、次の **ベース impact** を用いる（数値が大きいほど影響大）。

| 条件 | ベース impact |
|------|----------------|
| `organization_candidate` | **10** |
| `service_candidate` | **10** |
| `proper_noun_candidate` | **8** |
| `suspicious_number_or_role` | **6** |
| `suspicious_word` | **5** |
| `extract_unknown_points` 由来 `固有名詞` | **7** |
| `数値` | **5** |
| `主語` | **5** |
| 上記以外 | **4** |

※ `RISKY_TYPE_PRIORITY` で `mapped` に寄せるのではなく、**raw の `type` を上表で直接見る**（現行の `base_priority` と数値が一致しないことがあるが、MVP では **意図的に細分化**する）。

### 2.3 スコアの段階

- **ベース impact**：上表の **一段階**（4〜10）。
- **出現回数ボーナス**（§2.5）を加算した **合計を `impact` として固定**し、以降の `value` 計算に使う（小数は使わない）。

### 2.4 organization / service / proper_noun / suspicious_word の扱い

- 上表どおり **suspicious_word は最下位帯（5）**、organization / service は **最上位帯（10）**。
- これにより、同じ `risky_band`・同じ `idx` 近傍でも **DHR/THR/PHR が organization 等として付いていれば** 隣刑のような **suspicious_word より上**に付きやすい（その type が実際に入力へ含まれるかは候補生成側の仕様に依存する）。

### 2.5 出現回数（オプションだが MVP に含める）

- **目的**：diff spec の coverage を **入れずに**、「同じ略称が複数回出るほど重要」という **粗い proxy** を impact に足す。
- **数える場所**：`select_one_unknown_value_based` 呼び出し **内**、各 `candidate` ごとに `full_text` に対して実行。
- **代表トークン `token` の決定（固定ルール）**：
  1. `t = candidate["text"].strip()`。
  2. `t` から正規表現 **`[A-Za-z]{2,}`` にマッチする部分を **すべて**列挙（例: `THR`, `DHR`, `PHR`, `TOKIO`）。マッチがあれば、そのうち **`full_text` 内の出現回数が最大**のものを `token` とする（同率なら辞書順で先）。
  3. マッチがなければ、`t` の **先頭から最大 12 文字**を `token` とし、`full_text` における **部分文字列出現回数**（重複許容の単純カウント）を `count` とする。
- **ボーナス**：`bonus = min(3, max(0, count - 1))`（1回出現=0、2回=1、…、4回以上=3）。
- **`impact_total = base_impact + bonus`**（上限 **13** を超えないよう `min(13, impact_total)` でもよい）。

---

## 3. recoverability の詳細設計

### 3.1 定義（diff spec との対応）

- **recoverability が高い**＝ **ぼかして逃がしやすい**＝ **同じ impact 帯では質問を後回しにしてよい**。
- **スコアは整数 0〜10**。**高いほどぼかしやすい**（＝選ぶ価値は下がる）。

### 3.2 入力

| 入力 | 必須 |
|------|------|
| `candidate["type"]` | 必須 |
| `candidate["text"]` | 必須 |
| `full_text` | 必須（空のときは §3.4 の「文脈ウィンドウ」判定はスキップし、デフォルトのみ） |

### 3.3 判定ルール（段階）

**ステップA（デフォルト）**  
`type_raw` に応じて **ベース recoverability**：

| 条件 | ベース recoverability |
|------|------------------------|
| `organization_candidate` / `service_candidate` | **2**（ぼかしにくい） |
| `proper_noun_candidate` | **4** |
| `suspicious_number_or_role` / `数値` | **3** |
| `主語` | **4** |
| `固有名詞`（ルール抽出） | **5** |
| `suspicious_word` | **7**（誤変換・ノイズの可能性が高く、文脈で逃がしやすい） |

**ステップB（文脈ヒューリスティック）**  
`full_text` が空でないとき、`candidate["text"]` に対応する **文脈ウィンドウ**を取り、**いずれかに当てはまれば recoverability を +2**（上限 **10**）：

1. `window` の取得：`full_text` 内で `candidate["text"][:40]`（短ければ全文）を最初に出現する位置を `pos` とし、`full_text[max(0,pos-80):pos+len(text)+80]` を `window` とする。見つからない場合は `window = ""`。
2. `window` に以下の **部分文字列のいずれか**が含まれる（Unicode そのまま）：
   - `みたいに` / `みたいな` / `とか` / `例えば` / `例え` / `なんか`（口語・例示）
   - `比喩` / `勢い` / `雑談`（会議で稀だが明示的な場合）

**ステップC（キャップ）**  
`recoverability = min(10, base + bonus_context)`。

### 3.4 比喩・口語・例示・雑談的ノイズをどう下げるか

- **suspicious_word** 自体に **高いベース recoverability（7）** を付与し、さらに **ステップB** で **+2** しやすくする（比喩に埋もれた「隣刑」型）。
- **organization_candidate** は **ベース 2** のままステップBで **+2** しても **最大 4** 前後に留まり、**隣刑より下がりにくい**（先に聞く価値が残る）。

### 3.5 どの情報だけで判定するか

- **MVP は `type` + `text` + `full_text` のみ**（`reason` は使わない）。
- 前後文は **`full_text` からの部分文字列探索**で取得（別ファイル不要）。

---

## 4. 最終選定ロジック

### 4.1 value の算出式

- `impact` = §2 の `impact_total`（整数 4〜13 想定）。
- `recoverability` = §3 の最終値（整数 0〜10）。
- **`value = impact - recoverability`**（整数）。

### 4.2 最大 / 最小

- **`value` が最大**の候補を採用する（**最大化**）。

### 4.3 タイブレーク順（同じ `value` のとき）

現行 `select_one_unknown_prioritized` と同じキーを **昇順**で最小とする（数値が小さいほど優先）：

1. `risky_band`（0 が risky 由来）
2. `base_priority`（`TYPE_PRIORITY` にマップ後の値）
3. `idx`（`unknown_points` の配列インデックス）

`risky_band` / `base_priority` の定義は **現行コード**（`run_question_cycle_once.RISKY_TYPE_PRIORITY` + `generate_one_question.TYPE_PRIORITY`）をそのまま用いる。

### 4.4 `select_one_unknown_prioritized` の置き換え

- **削除せず**、ロジックを `tiebreak_key(idx, item) -> (risky_band, base_priority, idx)` として残し、**`select_one_unknown_value_based` 内**でタイブレークにのみ使用する。
- もしくは `select_one_unknown_prioritized` を `tiebreak_components` にリネームして内部利用（実装者の判断）。

---

## 5. データ構造

### 5.1 unknown candidate に追加する一時フィールド（メモリ上のみ）

JSON へは **書き戻さない**。選定中の dict にのみ付与（コピーしてから付与してもよい）：

| フィールド | 型 | 例 |
|------------|-----|-----|
| `_impact` | `int` | `11` |
| `_recoverability` | `int` | `9` |
| `_value` | `int` | `2` |

選ばれた要素を `build_user_friendly_question` に渡す前に、これら **キーを `pop` して削除**し、既存の `build_user_friendly_question` / LINE 連携の **入力形式を変えない**。

### 5.2 `question_result.json` に残すべきログ項目

`selection_audit` に **既存キーを維持**しつつ、以下を **追加**：

| キー | 内容 |
|------|------|
| `selection_mode` | 固定文字列 `"value_based_mvp_v1"` |
| `impact` | 選ばれた候補の `_impact` |
| `recoverability` | 選ばれた候補の `_recoverability` |
| `value` | 選ばれた候補の `_value` |
| `tiebreak_used` | `bool`（同 `value` が複数あったか） |

既存の `index_in_unknown_points` / `type_raw` / `risky_band` / `type_priority_rank` は **タイブレーク説明用に残す**。

### 5.3 デバッグしやすい出力

- **標準出力**（または `print`）：`job_id` 時に **上位3件**の `(idx, value, impact, recoverability, type_raw)` を一覧（任意フラグ `--debug-selection` でオンでも可）。
- **ファイル**：必須ではない。必要なら `question_selection_debug.json` を同一 `job_dir` に **任意**で追加（設計上は必須としない）。

---

## 6. 回帰確認観点

| 観点 | 確認内容 |
|------|----------|
| 「隣刑」 | `suspicious_word` + 比喩ウィンドウで **recoverability が高く**、**value が DHR 系より低く**なること（**実データで1回ログ確認**）。 |
| 「DHR/THR/PHR」 | `organization_candidate` 等で **impact が高く**、recoverability **低め**で **value が上位**になること。 |
| 固有名詞・数値系 | `extract_unknown_points` 由来の `固有名詞`/`数値`/`主語` が、**無条件で常に最上位**にならないこと（**value により**隣刑より上がる場合・下がる場合の両方を許容し、**異常な全滅**がないかを見る）。 |

---

## 7. 実装順

### 7.1 最小の1ステップ目

1. **`question_value_selection.py` を新規作成**し、`compute_impact` / `compute_recoverability` / `select_one_unknown_value_based` を **単体テスト可能な形**で実装する（`full_text=""` のフォールバック含む）。
2. **`run_question_cycle_once.main` の1行**を差し替え、`selection_audit` を拡張する。

### 7.2 次の確認方法

- 既存 `job` フォルダの `unknown_points.json` + `merged_transcript_ai.txt` を **保存済みコピー**で `run_question_cycle_once` を実行し、`question_result.json` の `selection_audit` と **デバッグ出力**を比較。
- **隣刑・PHR 系が同じファイルに含まれるジョブ**があればそれを優先して目視。

### 7.3 失敗しやすいポイント

- **候補生成側の `type` が実データで期待とズレる**（例：隣刑が `proper_noun_candidate` になる）→ impact 帯が上がりすぎる。**対策**：`suspicious_word` 以外でも `text` が短い（例: **文字数 ≤4**）かつステップBマッチなら recoverability を **追加 +2** する **逃げ**を入れるかどうかは実装時に1行で決める（本設計では必須としない）。
- **`full_text` が空**：出現回数ボーナスが効かず、差が縮まる → `context_text_path` の解決失敗に注意。
- **`generate_one_question.py` だけ**実行する運用があるなら、**第2段**で `choose_one_unknown` を統合する（本 MVP の成功条件外でもよいが、記載は残す）。

---

## 8. 参照

- `docs/question_selection_diff_spec.md`
- `run_question_cycle_once.select_one_unknown_prioritized`（現行タイブレーク）
- `extract_unknown_points.py`（候補の生成元）
- `run_question_cycle_once.RISKY_TYPE_PRIORITY`（旧拡張互換の type マッピング）
