# 改善タスク一覧（2025-01-23 更新）

現行パイプラインに対する改善計画。
実装の正はコード、要件の正は spec.md。本ファイルは優先度と状況の目安。

---

## 完了済み

- [x] AI補正を Claude 3.5 Sonnet 全文一括に移行 (`ad7aafb`)
- [x] 旧チャンク分割AI補正を廃止（overlap・ratio問題を根本解消）
- [x] チャンク分割方式の検討 → 不採用（ストリーミング方式を採用）
- [x] `ai_correct_text.py`: Claude API 呼び出しをストリーミング方式に変更
- [x] `httpx` のインポート追加、および Anthropic クライアントの timeout 設定を 900 秒に調整
- [x] Claude ストリーミングの再試行制御を追加（最大2回、5秒→10秒バックオフ）
- [x] Claude ストリーミングの進捗ログを間引き付きで追加（500文字ごと または 10秒ごと）
- [x] `filename_hints.py` を追加し、ファイル名由来の固有名詞ヒントを AI 補正へ注入
- [x] `.gitignore` で秘密情報保護 (`639767b`)
- [x] 旧v1パイプラインコード削除 (`ed926c3`)
- [x] Railway デプロイ正常動作確認
- [x] `docs/spec.md` 更新: 環境変数名の整合（DRIVE_FOLDER_ID等）、filename_hints.py・google_doc_hub.json・progress_tracker.py・Drive visible logs の記載追加
- [x] `review_risky_terms.py` をコードベースから削除

---

## Phase 1: 基盤整備（最優先）

### 1-1. Whisper API 移行
- [ ] ローカル faster_whisper → OpenAI Whisper API に置き換え
- [ ] チャンク分割アップロード対応（25MB制限）
- [ ] `filename_hints.py` の出力を `initial_prompt` パラメータに反映
- [ ] Railway 上での動作確認

### 1-2. Claude ストリーミング移行
- [ ] デプロイ後、同一ファイル（24,728文字）で再テスト
- [ ] テスト観点: ReadTimeout が解消されること、補正品質が変わらないこと

### 1-3. 不要ファイル削除
- [ ] `generate_one_question.py` の deprecated 明示 or 削除（本番は `run_question_cycle_once.py` に移行済み）

---

## Phase 2: 状態管理リファクタ

### 2-1. job_state.py 新設
- [ ] ジョブ状態を一元管理するモジュールを新設
- [ ] progress_tracker.py との統合・役割整理

### 2-2. drive_auto_run_forever.py リファクタ
- [ ] ノンブロッキングオーケストレーターとして再構築
- [ ] Suspend / Resume のフロー整備

---

## Phase 3: Webhook・LINE 整備

### 3-1. webhook_app.py 更新
- [ ] LINE 応答の3分類（回答 / 訂正 / 無関係）処理を実装
- [ ] Resume トリガーとの連携

### 3-2. LINE 通知フォーマット改善
- [ ] 高インパクト質問 + 仮説の送信フォーマット整備

---

## 中優先度（Phase 1-3 完了後）

### run_question_cycle_once.py
- [ ] AI応答の schema 検証強化
  - `selected_unknown` が dict でも `type` / `text` / `reason` が未検証
  - 必須キー欠落時はフォールバックへ回すべき
- [ ] `question_text` 空文字の扱い修正
  - `status=generated` + `text 空` が `none` 扱いになる（false negative）
- [ ] AI入力トリミング上限の見直し
  - transcript 12,000字 / unknown 25件 / 各220字
  - Claude 級なら大幅拡大可能

### extract_unknown_points.py
- [ ] カタカナ stopwords 追加（過検出対策）
  - `ミーティング` `プロジェクト` 等の一般語が候補に混入
  - 30-50語の除外リストで大幅改善見込み
- [ ] 時刻パターン除外（`N時` `N:NN`）

### export_minutes_to_google_docs.py
- [ ] Arial 11pt フォント統一
- [ ] H1 / H2 による7セクション構造スタイリング

---

## 低優先度

- [ ] `question_value_selection.py`: 日本語トークナイザ改善
  - 英字トークン偏重（`re.findall(r"[A-Za-z]{2,}")`)
  - bonus は max 3 cap なので実害限定的
- [ ] `run_job_once.py`: 処理後の WAV/チャンクファイル削除ステップ追加
  - ディスク使用量の蓄積対策

---

## ドキュメント整備

- [x] `.env.example` 更新
- [x] `README.md` 更新
- [x] `docs/todo.md` 更新
- [x] `docs/spec.md` 環境変数・機能記載の整合

---

## 変更履歴（この文書）

- **2025-01-23:** 3フェーズ計画（Whisper API移行→状態管理→Webhook整備）に全面再構成。review_risky_terms.py 削除完了を反映。spec.md 更新完了を反映。
- **2026-04-01:** `review_risky_terms.py` と `docs/status_2025-04-01.md` の削除、および spec.md の hub 保存場所・タイトル遷移・進捗管理責務の整理を反映。
- **2026-04-01:** Claude ストリーミング移行の実装完了を反映。`ai_correct_text.py` に 900 秒 timeout、再試行バックオフ、間引き進捗ログを追加。
- **2026-04-01:** `filename_hints.py` を完了済みに反映。新パイプライン基準で全面書き直し。
- **2026-04-01:** 新パイプライン基準で全面書き直し。旧v1タスク・進捗を削除し、監査結果ベースの改善リストに整理。
