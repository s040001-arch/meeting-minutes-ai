# Phase 10 引き継ぎ（2026-06）

**目的**: 誰が入っても現在地から作業を再開できる運用メモ。  
**設計思想・鉄の掟の詳細**: [minutes_design_philosophy.md](./minutes_design_philosophy.md) を参照（本文は重複させない）。

---

## 現在地（2026-06 時点）

- **Phase 10 編集者パス**。apply 本番 **10.2.3 の段1まで完了**、**段2デプロイ手前**。
- **本文を触る全経路を pinpoint 統一済み**（①④ `editor_apply` / ②③ `recorrect_from_line_answer`、LLM incorporate 撤去）。共通モジュール: `pinpoint_answer_apply.py`。
- **安全網**
  - Layer 1: `fact_class` routing（`edit_proposal_schema.refresh_proposal_routing`）
  - Layer 3: `fact_integrity_gate`
  - Layer 3b: `semantic_integrity_gate`（**claude-sonnet-4-6**）。「非同音一律 NG」ではなく **「正しい語が文脈で一意に確定するか」** で判定。
- **モデル**: 旧 Sonnet 4（`claude-sonnet-4-20250514`）は 3b から廃止済み。**他 9 モジュールの旧 ID 掃除は未完（Part C）**。
- **④**: `filler_garble` に加えフィラー・重複・言い直しまで拡張（`filler_garble_expand.py`）。人名ガード（tandem 削除後の `さ`/`で`/`は` スキップ）あり。
- **②safe / ③safe グルーピング**実装済み（同一着地のみまとめ、季央型は個別）。**LINE 送信側**（`run_question_cycle_once`）への `targets[]` 統合は未完。
- **164142**: ③ tier-A **8 件**（8数字→勝ち筋 等）を `answers_step3.json` 経由で `after_qa` に反映済み。
- **段1（164142）**: editor apply-only（`SEMANTIC=on`）実走・fact gate PASS・revert 0。成果物: `scripts/fixtures/phase10_2_3_164142_stage1/`（全文 diff・`editor_apply_report.json`）。①は妥当。**④は大原あ span 過削除・句読点ノイズ（、。等）を段2前に修正予定**（下記「進行中」）。

### 主要ファイル（参照用）

| 領域 | パス |
|------|------|
| 編集者 shadow/apply | `contextual_editor.py`, `editor_apply.py` |
| ①④ ゲート | `semantic_integrity_gate.py`, `fact_integrity_gate.py` |
| ②③ pinpoint | `pinpoint_answer_apply.py`, `recorrect_from_line_answer.py` |
| ③ export/apply | `question_bundle_step3.py`, `scripts/apply_phase10_step3_answers.py` |
| 164142 データ | `data/transcriptions/job_20260330_164142*`, `data/164142_after_qa.txt` |

---

## 進行中

- **Claude Code** が **段2前修正**（大原あ ④ span 是正・apply 後句読点 normalize）を **自律実施中**。
- **Cursor は当面触らない**: `editor_apply.py` / 句読点 normalize モジュール / ④ routing — 重複実装・コンフリト回避。
- 完了後: **ディレクター Claude レビュー** → **段2 GO/NOGO**。

---

## 残タスク

### 10.2.3 デプロイ（直近）

| 段 | 内容 |
|----|------|
| **段2** | 本番デプロイ + Railway `SEMANTIC_INTEGRITY_GATE_ENABLED=on`（2-d 方針） |
| **段3** | デプロイ直後に本番ジョブ 1 本。`editor_apply_report.json` の `semantic_reverted_count`・fact gate・missing_from_llm 件数を確認 |

段2前修正マージ後: 164142 で段1と同手順の diff 再確認 → GO なら段2へ。

### 164142 回答・品質

- **③ B48 件・C10 件** — 相原作業（`scripts/fixtures/phase10_step3_164142_answers_template.json` / `answers_step3.json`）。
- **優先2**: profile 非依存の人名誤認識検出（iPhone→相原 が漏れた根）。
- **優先3**: 数値+単位整合・hypothesis 生成漏れ・anomaly 取り違え。

### インフラ・横展開

- **Part C**: 旧 Sonnet ID 掃除（9 モジュール）。
- LINE 通数最適化・`run_question_cycle_once` への bundle / `targets[]` 統合。

---

## 不変境界線（触らない）

- `run_job_once` **CLI 契約・exit code**
- **Step 4.2** `learned_corrections` 語ペア
- **Drive 検知・ジョブ起動本体**
- **スキーマ後方互換**

---

## 判断基準（絶対遵守）

優先順: **事実の正しさ > 意味が通る > 工数最小**。

- [minutes_design_philosophy.md](./minutes_design_philosophy.md) の **鉄の掟**（事実は ①④ 禁止 → ②③）。
- **本文は pinpoint のみ**。LLM に織り込ませない（incorporate 経路は撤去済み）。
- 穴塞ぎでなく逆算。**迷ったら聞く**（②③）。

### 2-d 確定のエラー時挙動

- API 障害 → **ジョブ停止**
- `missing_from_llm` → **当該提案のみ保留・継続**

---

## 更新履歴

| 日付 | 内容 |
|------|------|
| 2026-06 | 初版。段1完了・Claude Code 段2前修正進行中を記録 |
