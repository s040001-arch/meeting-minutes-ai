# Phase 10 引き継ぎ（2026-06）

**目的**: 誰が入っても現在地から作業を再開できる運用メモ。  
**設計思想・鉄の掟の詳細**: [minutes_design_philosophy.md](./minutes_design_philosophy.md) を参照（本文は重複させない）。

---

## 現在地（2026-06 時点）

- **Phase 10 編集者パス**。**段3まで完了・本番 apply モード稼働中**（2026-06-22 相原GOで通常運転開始）。
  Railway production: `CONTEXTUAL_EDITOR_ENABLED=on` / `CONTEXTUAL_EDITOR_MODE=apply` / `SEMANTIC_INTEGRITY_GATE_ENABLED=on`。
  最新デプロイは Part C コミット（旧Sonnet 4 ID 9モジュール掃除済み）、ログ正常（`no_new_files` ポーリング継続、エラーなし）。
- **本文を触る全経路を pinpoint 統一済み**（①④ `editor_apply` / ②③ `recorrect_from_line_answer`、LLM incorporate 撤去）。共通モジュール: `pinpoint_answer_apply.py`。
- **安全網**
  - Layer 1: `fact_class` routing（`edit_proposal_schema.refresh_proposal_routing`）
  - Layer 3: `fact_integrity_gate`（amounts / schedule_tokens / participant / place の4カテゴリ別チェックに分割済み）
  - Layer 3b: `semantic_integrity_gate`（**claude-sonnet-4-6**）。「非同音一律 NG」ではなく **「正しい語が文脈で一意に確定するか」** で判定。
- **モデル**: 旧 Sonnet 4（`claude-sonnet-4-20250514`）は 3b から廃止済み。**他 9 モジュールの旧 ID 掃除は未完（Part C）**。
- **④**: `filler_garble` に加えフィラー・重複・言い直しまで拡張（`filler_garble_expand.py`）。人名ガード（tandem 削除後の `さ`/`で`/`は` スキップ）あり。
- **②safe / ③safe グルーピング**実装済み（同一着地のみまとめ、季央型は個別）。**LINE 送信側**（`run_question_cycle_once`）への `targets[]` 統合は未完。
- **164142**: ③ tier-A **8 件**（8数字→勝ち筋 等）を `answers_step3.json` 経由で `after_qa` に反映済み。
- **段1（164142）**: editor apply-only（`SEMANTIC=on`）実走・fact gate PASS・revert 0。成果物: `scripts/fixtures/phase10_2_3_164142_stage1/`（全文 diff・`editor_apply_report.json`）。①は妥当。**④は大原あ span 過削除・句読点ノイズ（、。等）を段2前に修正済み**（ディレクターレビュー round2 で完了）。

### コミット構成（ディレクターレビュー round2 で再構成済み）

段1の WIP は当初 3 コミットに分割されたが、`contextual_editor.py` が `semantic_integrity_gate.py` /
`filler_garble_expand.py` を import している一方、その2モジュールが (b) 側に入っており、
(a) だけデプロイすると `ModuleNotFoundError` で本番ジョブが即停止する欠陥が判明（同様に
`meeting_profile.augment_profile_with_transcript_participants` と `fact_integrity_gate` の
amounts/schedule/place 拡張も、どのコミットにも入っていない未コミット差分だった）。round2 で
以下のとおり再構成し、**各コミット単体で import・テストが通る**ことを worktree で確認済み：

| コミット | 内容 | 単体テスト |
|---|---|---|
| (a) ①④ apply 一式 | `edit_proposal_schema.py` / `contextual_editor.py` / `editor_apply.py` / `semantic_integrity_gate.py`（3b）/ `filler_garble_expand.py` / `fact_integrity_gate.py`（Layer 3）/ `meeting_profile.py`（参加者推定） + 164142 段1 fixtures | `pytest tests/test_contextual_editor_phase10.py tests/test_filler_garble_expand.py tests/test_semantic_integrity_gate.py` |
| (b) Cursor pinpoint / ②③ | `pinpoint_answer_apply.py` / `recorrect_from_line_answer.py` / `recognition_batch.py` / `question_bundle*.py` / `step3_anomaly_repair.py` / `phase10_answer_template.py` + step2/3 export/apply scripts | `pytest tests/test_recorrect_editor_pinpoint.py tests/test_step3_anomaly_repair.py tests/test_question_bundle.py tests/test_question_bundle_step3.py` |
| (c) 164142 ③ A8 fixtures + 段1再apply スクリプト | データのみ。本番コードなし | — |

**段2デプロイ範囲は (a) のみ**。(a) は単体で import・テストが通る自己完結ユニットになった
ため、段2の目的（①④ apply に 3b=`semantic_integrity_gate` を効かせる）に (b) は不要。
(b) は `recorrect_from_line_answer.py` を旧 `ai_correct_text.call_openai_incorporate_answer`
（LLM incorporate、鉄の掟で撤去対象）から `pinpoint_answer_apply.apply_answers` 主体へ置き換える
**別フェーズの変更**で、`run_job_once.py` の `step_5_4_recorrect_from_line_answer` から自動的に
呼ばれる本番経路に影響する。「1 Phase = 1 デプロイ・混ぜない」原則により、段2には含めず、
(b) は ②③ pinpoint 移行として別途レビュー・別デプロイとする。
(a) のみデプロイした場合、`recorrect_from_line_answer.py` は (b) 適用前の旧版（LLM incorporate
経路）のまま稼働する — これは現状の本番からの後退ではなく「未着手のまま」なので段2のブロッカーにはならない。
(c) はデータ fixture のみで本番コードを含まないため、デプロイ範囲には無関係（出しても無害・出さなくても無害）。

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

- **段2・段3完了**。job_142841 実データでの apply 実走・各種検証スクリプト（span alignment / garble fragment /
  semantic revert 挙動）で fact gate PASS・過削除0を確認済み。2026-06-22 相原GOで `CONTEXTUAL_EDITOR_MODE=apply`
  を本番で通常運転として継続稼働。
- **Cursor は当面触らない**: `editor_apply.py` / 句読点 normalize モジュール / ④ routing — 重複実装・コンフリト回避。

---

## 残タスク

### 10.2.3 デプロイ（直近）

| 段 | 内容 | 状態 |
|----|------|------|
| **段2** | 本番デプロイ（コミット (a) のみ）+ Railway `SEMANTIC_INTEGRITY_GATE_ENABLED=on`（2-d 方針） | 完了 |
| **段3** | `CONTEXTUAL_EDITOR_MODE=apply` 切替・実データ apply 実走確認 | 完了（2026-06-22 相原GOで通常運転開始） |

次の優先タスクは下記「164142 回答・品質」「インフラ・横展開」。

### 164142 回答・品質

- **③ raw 件数の正は 71 件（bundled 69 件）**。`scripts/fixtures/phase10_step3_164142_answers_template.json` の
  `count_raw: 67` は古い `edit_proposals.json` 時点のスナップショットで、その後 4 件の C-tier
  `ask_without_candidate`（盛んなお／意味は思って／試合相手／1の映像）が新規検出されて増えた。
  66 はテストメソッド名の残骸でどこにも assert されていない（`test_count_sixty_six_raw` 本体は 67 を assert、現状 green）。
  **③ B48・C10 の回答フローに進む前に**、fixture を現在の `edit_proposals.json` で再 export し、
  既存 A8 の `answer_text` を再マージしてから着手すること（fixture 自体は今回未更新）。
- **③ B48 件・C14 件**（再export後の値。旧 fixture の C10 は古い）— 相原作業
  （`scripts/fixtures/phase10_step3_164142_answers_template.json` / `answers_step3.json`、再export要）。
  **2026-06-23 追記**: 優先2/3検証で 164142 の `edit_proposals.json` を再生成済み（旧版は
  `edit_proposals.json.bak_pre_priority23` に退避）。proposal 件数が変わっているため、
  B48/C14 着手前の再export はこの新 `edit_proposals.json` を基準にすること。
- **優先2・優先3: 完了（2026-06-23）**。`meeting_profile.py`（`SYSTEM_OWNER_NAME` を
  filename由来participantsへ常時マージ）/ `fact_classify.py`（数値+単位ガードを固定単位リストから
  「数字に直結する漢字カウンタ」構造判定へ一般化）/ `contextual_editor.py`（プロンプトに
  「参加者リストは不完全前提」「単位不整合は文脈の対象で判断」のガイダンス追加）。
  164142 で再shadow検証: 修正前は提案ゼロだった `iPhone さん` を `proper_noun`/`ask_without_candidate`
  で新規検出、`1000車` は `uncertain` から `numeric`/`ask_with_candidate`（hypothesis=`1000社`）に改善。
  本文不変・fact gate PASS・テスト138件green（`tests/test_meeting_profile.py` 新規、
  `tests/test_contextual_editor_phase10.py` に2件追加）。検証スクリプト:
  `scripts/verify_priority2_3_164142_shadow.py` / 結果: `scripts/fixtures/priority2_3_164142_shadow_verify.json`。
  ローカル実行時は `.env` の `GOOGLE_OAUTH_TOKEN_JSON` が失効しており world_knowledge 注入なしで検証
  （Railway本番は Drive ポーリングが正常稼働中のため別トークンで問題なし）。ディレクターレビュー待ち。

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
| 2026-06-20 | ディレクター round2: 3b (`semantic_integrity_gate`) が段2デプロイ対象から漏れる欠陥を検出・修正。3コミットを (a) ①④apply自己完結 / (b) ②③pinpoint / (c) ③fixtures に再構成（git push 前のローカル6コミットを安全に reset --soft して再分割）。段2デプロイ範囲を (a) のみに確定。③ raw 件数を 71（bundled 69）に確定、旧 fixture の 67 は stale と記録。 |
| 2026-06-22 | 段3完了。相原GOで Railway `CONTEXTUAL_EDITOR_MODE=apply` に切替・通常運転開始。最新デプロイ（Part C: 旧Sonnet 4 ID 9モジュール掃除）健全稼働を確認。 |
| 2026-06-23 | 優先2（profile非依存の人名誤認識検出）・優先3（数値+単位の文脈ベース不整合検出）を修正。164142再shadowで`iPhoneさん`新規検出・`1000車`のhypothesis改善を確認。本文不変・テスト138件green。ディレクターレビュー待ち。 |
