# 改善タスク一覧（2026-04-01 更新）

現行パイプライン (`run_job_once.py` 16ステップ) に対する改善候補。
実装の正はコード。本ファイルは優先度と状況の目安。

---

## 完了済み

- [x] AI補正を Claude 3.5 Sonnet 全文一括に移行 (`ad7aafb`)
- [x] 旧チャンク分割AI補正を廃止（overlap・ratio問題を根本解消）
- [x] `.gitignore` で秘密情報保護 (`639767b`)
- [x] 旧v1パイプラインコード削除 (`ed926c3`)
- [x] Railway デプロイ正常動作確認

---

## 中優先度

### run_question_cycle_once.py

- [ ] AI応答の schema 検証強化
  - `selected_unknown` が dict でも `type` / `text` / `reason` が未検証
  - 必須キー欠落時はフォールバックへ回すべき
- [ ] `question_text` 空文字の扱い修正
  - `status=generated` + `text 空` が `none` 扱いになる（false negative）
  - 例外扱いでフォールバックへ回すべき
- [ ] AI入力トリミング上限の見直し
  - transcript 12,000字 / unknown 25件 / 各220字
  - GPT-4o / Claude 級なら大幅拡大可能

### review_risky_terms.py

- [ ] `position_ratio` の実装追加
  - spec上は定義済みだが実装が見当たらない
  - `question_value_selection.py` の `late_document_bonus` が機能しない
  - GPTプロンプトに `position_ratio` 出力を追加する

### extract_unknown_points.py

- [ ] カタカナ stopwords 追加（過検出対策）
  - `ミーティング` `プロジェクト` 等の一般語が候補に混入
  - 30-50語の除外リストで大幅改善見込み
- [ ] 時刻パターン除外（`N時` `N:NN`）
  - `10時くらいに` 等の誤検出を防ぐ

### generate_one_question.py

- [ ] 旧ロジック残存の整理
  - 本番は `run_question_cycle_once.py` に移行済み
  - deprecated 明示 or 削除

---

## 低優先度

- [ ] `question_value_selection.py`: 日本語トークナイザ改善
  - 英字トークン偏重（`re.findall(r"[A-Za-z]{2,}")`)
  - bonus は max 3 cap なので実害限定的
- [ ] `run_job_once.py`: Step 6.3 後の WAV/チャンク削除ステップ追加
  - ディスク使用量の蓄積対策

---

## ドキュメント整備

- [x] `.env.example` 更新
- [x] `README.md` 更新
- [x] `docs/todo.md` 更新
- [ ] `docs/spec.md` の AI補正セクション更新（旧チャンク記述の修正）

---

## 変更履歴（この文書）

- **2026-04-01:** 新パイプライン基準で全面書き直し。旧v1タスク・進捗を削除し、監査結果ベースの改善リストに整理。
