# ディレクター指示 — 段2前ゲート（3点必須）

**対象**: Claude Code（ターミナル CLI）  
**状態**: 段2前修正の中身は承認済み。以下3点完了まで **段2に進まない**。  
**段2実行**: 相原 GO まで **本番デプロイ・SEMANTIC=on は実行しない**。

---

## 承認済み（再実装不要）

- 大原あ一般化: `_strip_leading_clause_connector`（`edit_proposal_schema.py`）
- 句読点 normalize: `normalize_editor_delete_punctuation`（`editor_apply.py`）
- ④ idempotent ガード: `supplemental_filler_expand` 再展開防止（`contextual_editor.py`）
- 164142 再 apply: applied 31, semantic/fact revert 0, gate PASS
- 設計思想準拠（mechanical の `cleanup_common_noise` とは分離）

---

## タスク1: 「合っています」#B 安全確認（鉄の掟・必須）

最新 diff で #B / #C を hunk 付きで報告すること。

**参照**: `scripts/fixtures/phase10_2_3_164142_stage1/phase10_2_3_164142_editor_apply_full.diff`

### #B（残存必須）

同意→データ投入の意味ある応答。**消えていたら鉄の掟違反**。

期待 hunk（after でも行頭「合っています」が残る）:

```diff
-合っていますそこにデータ入れるだけじゃないですけど、…よしかな…
+合っていますそこにデータ入れるだけじゃないですけど、…（よしかなのみ削除）…
```

### #C（削除のみ・OK）

言い淀み stutter。ここだけ「合っています」が消える。

期待 hunk:

```diff
-…ピックアップしてて、合っていますまこの中からどこ喋るか?…
+…ピックアップしてて、まこの中からどこ喋るか?…
```

### 報告形式

1. #B: 残存 YES/NO + diff 該当行引用
2. #C: 削除のみ YES/NO + diff 該当行引用
3. mechanical / after 全文で `合っています` grep 件数（期待: before 3 / after 2）

---

## タスク2: `test_count_sixty_six_raw` 切り分け（赤を放置しない）

**失敗**: `tests/test_question_bundle_step3.py::test_count_sixty_six_raw`  
- テスト期待: `meta.count_raw == 66`  
- fixture 実値: `scripts/fixtures/phase10_step3_164142_answers_template.json` → `count_raw: 67`

### 切り分け

| 判定 | 意味 | 対応 |
|------|------|------|
| **A** | fixture/test 更新漏れ（③ raw が正当に 66→67 等） | 正しい count に test/fixture を更新して緑 |
| **B** | ③が想定外に消えた/増えた | 原因調査（export / bundle / anomaly repair） |

### 手順

1. `edit_proposals.json` から `ask_without_candidate` を再カウント（raw）
2. `scripts/export_phase10_step3_answers_template.py` 再実行し `meta.count_raw` と突合
3. A なら test 期待値（必要なら fixture meta）を正値に更新
4. B なら原因特定 → 修正 or ディレクターへエスカレ（緑にする前に判断）

### 完了条件

```bash
python -m pytest tests/test_question_bundle_step3.py -q
```

→ PASS

### 報告

- A or B
- 正しい `count_raw` 値
- 変更ファイル一覧

---

## タスク3: コミット分割（段2前・最重要）

設計思想「1 Phase = 1 デプロイ、混ぜない」。未コミット WIP を **3 コミット**に分割。各コミット後に関連テスト緑。

### (a) Claude Code 段2前修正（①④ apply 系）— 1 commit

**含める**:

- `edit_proposal_schema.py`
- `contextual_editor.py`
- `editor_apply.py`
- `tests/test_contextual_editor_phase10.py`
- `tests/test_filler_garble_expand.py`
- `scripts/fixtures/phase10_2_3_164142_stage1/`（最新 diff / report / meta）

**含めない**: pinpoint / ③ apply / Cursor 系

**コミット後**:

```bash
python -m pytest tests/test_contextual_editor_phase10.py tests/test_filler_garble_expand.py -q
```

### (b) Cursor pinpoint 系（②③）— 1 commit

**含める**:

- `pinpoint_answer_apply.py`
- `recorrect_from_line_answer.py`
- `recognition_batch.py`（「現状維持」keep）
- `question_bundle.py`, `question_bundle_step3.py`, `step3_anomaly_repair.py`
- `phase10_answer_template.py`
- `filler_garble_expand.py`, `semantic_integrity_gate.py`（pinpoint/③ 経路で使うもの）
- `tests/test_recorrect_editor_pinpoint.py`, `tests/test_step3_anomaly_repair.py`, `tests/test_question_bundle.py`, `tests/test_question_bundle_step3.py`, `tests/test_semantic_integrity_gate.py`
- `scripts/apply_phase10_step2_answers.py`, `scripts/apply_phase10_step3_answers.py`
- `scripts/export_phase10_step2_answers_template.py`, `scripts/export_phase10_step3_answers_template.py`

**コミット後**:

```bash
python -m pytest tests/test_recorrect_editor_pinpoint.py tests/test_step3_anomaly_repair.py tests/test_question_bundle_step3.py -q
```

### (c) ③ A8 + fixtures + 段1スクリプト — 1 commit

**含める**:

- `scripts/fixtures/phase10_step3_164142_answers_template.json`（A8 `answer_text` 記入済み）
- `scripts/fixtures/phase10_step3_164142_answers_review.md`（あれば）
- `data/fixtures/job_164142_step3/answers_step3.json`（あれば）
- `scripts/run_phase10_2_3_stage1_164142_apply_diff.py`

**data/164142_after_qa.txt**: 通常は gitignore 想定なら **含めない**（fixtures のみ）。

### 最終確認

```bash
python -m pytest tests/ -q
```

→ **1 failure も不可**（タスク2 含む）

### 報告

- 3 コミットの SHA + メッセージ
- **段2本番デプロイに含める範囲**を明示: `(a) のみ` / `(a)+(b)` / `(a)+(b)+(c)` のどれか
- 不変境界触っていないか（下記）

---

## 不変境界線（触っていないか再確認）

- `run_job_once` CLI 契約・exit code
- Step 4.2 `learned_corrections` 語ペア
- Drive 検知・ジョブ起動本体
- スキーマ後方互換

---

## 完了報告テンプレ

```
## 段2前ゲート完了報告

### タスク1（合っています）
- #B 残存: YES/NO — [hunk引用]
- #C 削除のみ: YES/NO — [hunk引用]
- grep: before N / after M

### タスク2（test_count_sixty_six_raw）
- 判定: A or B
- 正しい count_raw: N
- pytest test_question_bundle_step3: PASS

### タスク3（コミット分割）
- (a) SHA: ... — message: ...
- (b) SHA: ... — message: ...
- (c) SHA: ... — message: ...
- 段2に出す範囲: (a) / (a)+(b) / 全部
- pytest tests/: N passed, 0 failed

### その他
- docs/phase10_handoff.md 更新: [一行要約]
- 段2未実行（本番デプロイ・SEMANTIC=on なし）
```

---

## 参照

- 引き継ぎ: `docs/phase10_handoff.md`
- 設計思想: `docs/minutes_design_philosophy.md`
