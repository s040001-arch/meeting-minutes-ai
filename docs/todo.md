# 改善タスク一覧（2026-04-02 更新②）

現行パイプラインに対する改善計画。
実装の正はコード、要件の正は `spec.md`。本ファイルは優先度と進捗の目安。

当面の運用方針:
- まずは実装を進める
- 検証は後追いでまとめて進める
- 各タスクは `実装` と `検証` を分けて管理する

定義:
- `実装`: コードまたはドキュメント反映が完了している状態
- `検証`: 実データ・実運用相当・デプロイ環境などで期待どおり動くことを確認した状態

---

## 実装済み・検証待ち

### AI補正 / 不明点検出 / 議事録生成

- [x] 実装: AI補正を Claude 全文一括方式へ移行
- [ ] 検証: 長文データで補正品質と安定性を再確認

- [x] 実装: 旧チャンク分割AI補正を廃止
- [ ] 検証: 旧方式に依存した副作用が残っていないか確認

- [x] 実装: `ai_correct_text.py` を Claude ストリーミング方式へ変更
- [ ] 検証: 同一長文ファイルで `ReadTimeout` が解消されることを確認

- [x] 実装: `httpx` 導入と Anthropic timeout 900 秒化
- [ ] 検証: Railway 上で実際に長時間処理が完走することを確認

- [x] 実装: Claude ストリーミングの再試行制御を追加（最大2回、5秒→10秒バックオフ）
- [ ] 検証: 一時的な API エラー時に意図どおり再試行されることを確認

- [x] 実装: Claude ストリーミングの進捗ログ間引きを追加（500文字ごと または 10秒ごと）
- [ ] 検証: 実運用ログ量が過剰でないことを確認

- [x] 実装: Step⑨ AI不明点検出を実装し、Claude 検出結果を `unknown_points.json` に保存
- [ ] 検証: 実データで過検出・取りこぼしの傾向を確認

- [x] 実装: Step⑩ で Regex 検出結果と Claude 検出結果をマージ
- [ ] 検証: 重複や不要候補が過剰に増えないことを確認

- [x] 実装: Step⑪ 議事録生成を Claude ベースへ更新し、spec の7セクション構成へ整形
- [ ] 検証: 実データで出力品質と根拠逸脱の有無を確認

- [x] 実装: `filename_hints.py` を追加し、ファイル名由来のヒントを AI 補正へ注入
- [ ] 検証: 人名・社名の補正精度が改善するか確認

### 質問選定 / 回答反映

- [x] 実装: 未回答の `unknown_points` のみを次質問候補として扱うよう更新
- [ ] 検証: 回答済み項目が再質問されないことを確認

- [x] 実装: 未回答項目がゼロなら LINE に完了メッセージを返すよう更新
- [ ] 検証: 質問終了時に完了通知が意図どおり送られることを確認

- [x] 実装: LINE 回答受信時点で対象の `unknown_points` を即 `answered` に更新
- [ ] 検証: 回答受信直後に `unknown_points.json` が更新されることを確認

- [x] 実装: 回答反映後に残りの不明点を再評価し、周辺文脈まで実質解決した候補は落とす処理を追加
- [ ] 検証: 追加質問が減る一方で、必要な質問は残ることを確認

- [x] 実装: LINE 受信文から回答情報と修正依頼を同時抽出できるよう更新
- [ ] 検証: 1通に回答と訂正が混在するケースでも両方反映されることを確認

- [x] 実装: LINE 質問文を短文化し、Google Docs リンク付きフォーマットへ更新
- [ ] 検証: 実運用で読みやすく、回答しやすい文面になっているか確認

### ログ / 運用性

- [x] 実装: `processing_visible_log.txt` と Drive visible log によるステップ単位の可視ログを追加
- [ ] 検証: Railway 上で停止箇所の特定に十分使えることを確認

### 補正品質

- [x] 実装: `mechanical_correct_text.py` に助詞重複・短い定型句の連続・明らかなノイズ置換の機械補正を追加
- [ ] 検証: Google Docs 上の赤線や不自然表現が実データでどの程度減るか確認

- [x] 実装: 回答反映後の再開ループで Google Docs タイトルを最終的にプレフィックスなしへ戻すよう更新
- [ ] 検証: 再開完了後に `【議事録生成中】` などの状態が残らないことを確認

### ナレッジ蓄積 / Google Sheets 連携

- [x] 実装: `knowledge_sheet_store.py` を新設し、Google Sheets API（サービスアカウント認証）で知識メモを読み書き
- [ ] 検証: 手動編集を含めて最新ナレッジを安定して読み書きできることを確認

- [x] 実装: LINE 回答受信時に Claude を呼び出し、ナレッジの蓄積価値判定・既存ナレッジ統合・Sheets 全体更新を実装
- [ ] 検証: 回答内容が意図どおり Google Sheets のナレッジメモへ統合されることを確認

- [x] 実装: `seed_knowledge.py` で初期ナレッジ（Precena 関連60件）を Sheets に投入
- [ ] 検証: Sheets 上のナレッジが AI 補正で意図どおり参照されることを確認

- [x] 実装: Step⑧ の AI 補正プロンプトに Google Sheets のナレッジメモ全件を参考知識として注入
- [ ] 検証: ナレッジ注入により補正精度が改善することを確認

### job_context / per-job コンテキスト注入

- [x] 実装: `job_context.py` を新設し、ジョブディレクトリの `context.json`（参加者・企業名・議題）を AI 補正プロンプトへ注入
- [ ] 検証: context.json を置いたジョブで固有名詞補正精度が改善することを確認

### railway_bootstrap / 認証情報管理

- [x] 実装: `railway_bootstrap.py` に `GOOGLE_SERVICE_ACCOUNT_JSON` → `credentials_service_account.json` の復元処理を追加
- [ ] 検証: Railway デプロイ後に Sheets API 認証が正常に通ることを確認

### LINE webhook 非同期化

- [x] 実装: `/callback` を即座に HTTP 200 返却する構成に変更（FastAPI BackgroundTasks）
- [x] 実装: `handle_user_input()` を BackgroundTasks で非同期実行
- [x] 実装: LINE 応答を reply API（replyToken）から push message API（LINE_USER_ID）に切り替え
- [ ] 検証: Opus 呼び出しを含む処理が reply token 失効（30秒）の制約を受けないことを確認

### AI補正パイプライン Opus 一括補正（Step⑧）

- [x] 実装: マスキング→JSON検出→str.replace の多段処理を廃止
- [x] 実装: 機械補正後テキストを Claude 4 Opus に一括で渡す単一呼び出しに置き換え
- [x] 実装: `_build_opus_correction_system_prompt()` に補正ルール・context.json・ナレッジメモ・filename_hints を一括注入
- [x] 実装: モデルを `claude-opus-4-20250514` に更新（`ANTHROPIC_CORRECTION_MODEL` 環境変数でオーバーライド可）
- [ ] 検証: 実データで補正品質 85% 以上を確認

### spec.md / ドキュメント整備（Opus 移行対応）

- [x] 実装: `docs/spec.md` を Opus 移行・非同期化・パイプライン簡素化・品質目標 85% に合わせて更新
- [x] 実装: `docs/spec.md` に Step⑰ ナレッジ蓄積・Step⑨ 重複防止・Knowledge Sheet A/B 列仕様を追記
- [ ] 検証: 実装との差分が残っていないか棚卸し確認

### Step⑨ AI不明点検出の本格実装

- [x] 実装: `detect_unknown_points.py` を新設し、Claude 4 Opus で補正済みテキストから不明点を最大10件検出
- [x] 実装: `run_job_once.py` で Step 4.35 として `detect_unknown_points()` を呼び出し（従来の空リストを置き換え）
- [x] 実装: `run_resume_from_step7.py` で Step 9 として `detect_unknown_points()` を呼び出し
- [x] 実装: 過去の回答済み不明点（`answered_items`）をプロンプトに含め、重複質問を抑制
- [ ] 検証: 実データで過検出・取りこぼしの傾向を確認（旧 Regex 専用時との比較）
- [ ] 検証: 回答済み情報が正しく「既知情報」としてプロンプトに反映されることを確認

### 質問重複送信防止・ステータス保全

- [x] 実装: `run_question_cycle_once.py` で LINE 送信後に選定した不明点を `status: asked` にマーク
- [x] 実装: `_filter_pending_unknown_points()` を `asked` 項目も除外するよう拡張（回答待ち質問の再送防止）
- [x] 実装: `restore_known_statuses()` を `run_job_once.py` に追加し、再補正サイクルで `answered`/`asked` ステータスを保全
- [x] 実装: `run_resume_from_step7.py` で Regex マージ後に `restore_known_statuses()` を適用
- [ ] 検証: 送信済み質問が次サイクルで再送されないことを確認
- [ ] 検証: LINE 回答後の再補正サイクルで、回答済みフラグが `unknown_points.json` に引き継がれることを確認

### ドキュメント・構成整理

- [x] 実装: `review_risky_terms.py` をコードベースから削除
- [ ] 検証: 参照残りや運用影響がないことを最終確認

- [x] 実装: `README.md` と設計メモの残存言及を整理
- [ ] 検証: 古い前提の記述が残っていないか確認

---

## 実装・検証とも完了

- [x] 実装: `.gitignore` で秘密情報保護を整理
- [x] 検証: Git 管理に含めたくないファイルを通常コミット対象から外せる状態を確認

- [x] 実装: 旧 v1 パイプラインコードを削除
- [x] 検証: 削除後も現行パイプライン前提で開発を継続できる状態を確認

- [x] 実装: Railway デプロイ経路を整備
- [x] 検証: Railway デプロイ正常動作確認

---

## 未実装

### Step⑰ ナレッジ蓄積のパイプライン組み込み（最優先）

spec.md では Step⑪完了直後に Step⑰ ナレッジ蓄積を実行する仕様だが、現在パイプラインには組み込まれていない。

- [ ] 実装: ナレッジ蓄積ロジックを専用関数 or モジュール（`accumulate_knowledge_step17.py` 等）に切り出す
  - Knowledge Sheet 全件読み込み → Claude Opus に回答群と渡して統合判断 → Sheets 全体書き戻し
  - 既存の `knowledge_sheet_store.py` の `merge_answer_into_knowledge_store()` を参考にするか、統合を検討
- [ ] 実装: `run_job_once.py` の Step⑪完了直後（議事録生成後）に Step⑰ 呼び出しを追加
- [ ] 実装: `run_resume_from_step7.py` の Step⑪相当完了直後にも同様に Step⑰ 呼び出しを追加
- [ ] 検証: ナレッジ蓄積が実行されること、かつ Knowledge Sheet に整合のとれた内容が書き戻されることを確認
- [ ] 検証: Step⑰ が失敗しても以降のパイプライン（Step⑫〜）が止まらないことを確認

### answers.json の設計と実装（Step⑰ の前提）

spec.md では「answers.json（現在のジョブで得られた回答の蓄積）」を Step⑰ が参照するが、この仕組みが未実装。現在は `unknown_points.json` の `answered` エントリが唯一の回答記録。

- [ ] 実装: ジョブディレクトリに `answers.json`（ジョブ内回答の累積ログ）を設計・定義する
  - 例: `[{"question_text": "...", "answer_text": "...", "answered_at": "..."}]`
- [ ] 実装: `webhook_app.py` の回答受信時に `answers.json` へ追記する
- [ ] 実装: `detect_unknown_points.py` が `unknown_points.json` の `answered` 項目と合わせて `answers.json` も参照するよう更新（またはどちらか一方に統一）
- [ ] 検証: 複数回の Q&A を経た後、`answers.json` に全回答が記録されていることを確認

### Knowledge Sheet B列（カテゴリ）の読み書き対応

spec.md で A列（自由記述）・B列（カテゴリ）の2列構造を定義したが、現在の実装は A列のみを使用している。

- [ ] 実装: `knowledge_sheet_store.py` の読み込み処理で B列（カテゴリ）も取得するよう更新
- [ ] 実装: ナレッジ蓄積時（Step⑰）に Claude がカテゴリを返す場合、B列にも書き込む
- [ ] 実装: AI補正（Step⑧）・不明点検出（Step⑨）のプロンプトでカテゴリ情報を活用する（任意）
- [ ] 検証: B列が正しく読み書きされ、既存の A列処理に影響が出ないことを確認

### Phase 1: 基盤整備（最優先）

#### 1-1. Whisper API 移行

- [ ] 実装: ローカル `faster_whisper` → OpenAI Whisper API に置き換え
- [ ] 検証: Railway 上で安定動作することを確認

- [ ] 実装: チャンク分割アップロード対応（25MB制限）
- [ ] 検証: 大きい音声でも失敗せず完走することを確認

- [ ] 実装: `filename_hints.py` の出力を `initial_prompt` パラメータへ反映
- [ ] 検証: Whisper 初期認識で固有名詞精度が改善することを確認

#### 1-2. Claude ストリーミング再検証

- [ ] 実装: 追加改修なし（現時点では実装済み）
- [ ] 検証: 同一ファイル（24,728文字）で再テストし、品質変化も確認

#### 1-3. 不要ファイル削除

- [ ] 実装: `generate_one_question.py` の deprecated 明示または削除
- [ ] 検証: 他モジュールの依存が残らないことを確認

### Phase 2: 状態管理リファクタ

#### 2-1. `job_state.py` 新設

- [ ] 実装: ジョブ状態を一元管理するモジュールを新設
- [ ] 検証: `progress_tracker.py` との責務分離が破綻しないことを確認

- [ ] 実装: `progress_tracker.py` との統合・役割整理
- [ ] 検証: 状態更新の競合や抜け漏れがないことを確認

#### 2-2. `drive_auto_run_forever.py` リファクタ

- [ ] 実装: ノンブロッキングオーケストレーターとして再構築
- [ ] 検証: Drive 監視が常時安定することを確認

- [ ] 実装: Suspend / Resume フロー整備
- [ ] 検証: 中断再開時に状態不整合が起きないことを確認

### Phase 3: Webhook・LINE 整備

#### 3-1. `webhook_app.py` 更新

- [ ] 実装: 回答抽出 / 訂正抽出のプロンプト精度改善
- [ ] 検証: 誤抽出や取りこぼしが実運用上許容範囲か確認

- [x] 実装: Resume トリガーとの連携
- [ ] 検証: 回答受信後の自動再開が安定することを確認

#### 3-2. LINE 通知フォーマット改善

- [x] 実装: 高インパクト質問 + Docs リンク付き送信フォーマット整備
- [ ] 検証: ユーザーが答えやすい文面になっているか確認

### 中優先度（Phase 1-3 完了後）

#### `run_question_cycle_once.py`

- [ ] 実装: AI応答の schema 検証強化
- [ ] 検証: 必須キー欠落時に安全にフォールバックすることを確認

- [ ] 実装: `question_text` 空文字の扱い修正
- [ ] 検証: `status=generated` なのに実質 `none` 扱いになる誤判定がないことを確認

- [ ] 実装: AI入力トリミング上限の見直し
- [ ] 検証: 入力拡大後も精度・速度・コストが許容範囲か確認

#### `extract_unknown_points.py`

- [ ] 実装: カタカナ stopwords 追加（過検出対策）
- [ ] 検証: 一般語の誤検出が十分減ることを確認

- [ ] 実装: 時刻パターン除外（`N時` `N:NN`）
- [ ] 検証: 時刻由来のノイズ検出が減ることを確認

#### `export_minutes_to_google_docs.py`

- [ ] 実装: Arial 11pt フォント統一
- [ ] 検証: Google Docs 上で期待どおり反映されることを確認

- [ ] 実装: H1 / H2 による7セクション構造スタイリング
- [ ] 検証: Docs 上で見出し構造が使いやすいことを確認

### 低優先度

- [ ] 実装: `question_value_selection.py` の日本語トークナイザ改善
- [ ] 検証: 英字偏重が減り、選定品質が改善することを確認

- [ ] 実装: `run_job_once.py` に処理後の WAV / チャンク削除ステップ追加
- [ ] 検証: ディスク使用量が抑制され、再処理に必要な情報は失われないことを確認

- [ ] 実装: 中間生成物の保存ルール整理と自動削除
- [ ] 検証: 必要ファイルを残しつつ、`data/google_docs_export/` などの肥大化を防げることを確認

---

## ドキュメント整備

- [x] 実装: `.env.example` 更新
- [ ] 検証: 現行環境変数セットと齟齬がないか確認

- [ ] 実装: `.env` / `.env.example` にナレッジ用スプレッドシートIDの設定項目を追加する
- [ ] 検証: Railway / ローカル双方で設定値を正しく参照できることを確認

- [x] 実装: `README.md` 更新
- [ ] 検証: 初見ユーザー向け説明として破綻がないか確認

- [x] 実装: `docs/todo.md` 更新
- [ ] 検証: 実装/検証の管理ルールとして使いやすいか運用しながら確認

- [x] 実装: `docs/spec.md` の環境変数・機能記載を整合
- [ ] 検証: 現行コードとの差分が残っていないか確認

---

## Opus 移行計画

| Phase | 内容 | 状態 |
|-------|------|------|
| Phase 1 | LINE webhook 非同期化 | 実装完了・検証待ち |
| Phase 2 | AI補正パイプライン簡素化（Opus 一発補正） | 実装完了・検証待ち |
| Phase 3 | 品質テスト | 未実施 |

### Phase 3: 品質テスト

- [ ] 検証: 同一音声データで補正結果を評価し、品質 85% 以上を確認
- [ ] 検証: 処理時間・コスト・品質のトレードオフを記録

---

## 変更履歴（この文書）

- **2025-01-23:** 3フェーズ計画（Whisper API移行→状態管理→Webhook整備）に全面再構成。`review_risky_terms.py` 削除完了を反映。`spec.md` 更新完了を反映。
- **2026-04-01:** `review_risky_terms.py` と `docs/status_2025-04-01.md` の削除、および `spec.md` の hub 保存場所・タイトル遷移・進捗管理責務の整理を反映。
- **2026-04-01:** Claude ストリーミング移行の実装完了を反映。`ai_correct_text.py` に 900 秒 timeout、再試行バックオフ、間引き進捗ログを追加。
- **2026-04-01:** Step⑨ AI不明点検出を実装。Claude の high-guess 検出結果を `unknown_points.json` に保存し、Step⑩ Regex 検出とマージするよう更新。
- **2026-04-01:** Step⑪ 議事録生成を Claude ベースへ移行。`generate_minutes_other_sections.py` で要約セクションを生成し、7 セクション構成の Markdown を出力するよう更新。
- **2026-04-01:** `filename_hints.py` を完了済みに反映。新パイプライン基準で全面書き直し。
- **2026-04-01:** 回答反映フローについて、回答受信時の `answered` 更新と、回答後の不明点再評価タスクを反映。
- **2026-04-01:** 中間生成物の保存ルール整理タスクを追加。Docs dry-run 出力と再生成可能ファイルの削除方針を今後詰める。
- **2026-04-01:** `todo.md` を「実装」と「検証」を分けて管理する形式へ再構成。
- **2026-04-01:** LINE 受信を「3分類」から「回答情報 / 修正依頼の情報抽出」前提へ整理。可視ログ、質問文改善、機械補正強化、再開時の Docs タイトル戻しを反映。
- **2026-04-02:** Opus 移行 Phase 1（LINE webhook 非同期化・push API 切替）および Phase 2（Step⑧ Opus 一括補正・マスキング多段処理廃止）の実装完了を反映。`knowledge_sheet_store.py`・`job_context.py`・`seed_knowledge.py` の追加、`railway_bootstrap.py` の service account 対応、`spec.md` の Opus 移行・非同期化仕様への更新を反映。「未実装」から完了済み項目を「実装済み・検証待ち」に移動。
- **2026-04-02②:** Step⑨ AI不明点検出の本格実装（`detect_unknown_points.py` 新設・パイプライン組み込み）、質問重複送信防止（`asked` ステータス管理）、再補正サイクルでの回答済みステータス保全（`restore_known_statuses()`）の実装完了を反映。`spec.md` の Step⑰ ナレッジ蓄積・Knowledge Sheet A/B 列仕様・Step⑨ 重複防止仕様を更新。Step⑰ パイプライン組み込み・answers.json・B列対応を新規「未実装」として追加。
