# MVP タスク一覧・進捗（最新版・2026-03）

新規チャット（指示役）向け。**実装の正はコード**と `data/.../e2e_run_log.txt`。本ファイルは優先度と完了状況の目安。

---

## 進捗サマリ

| 領域 | 状態 | メモ |
|------|------|------|
| Drive 接続・新規検知・DL | 実装済 | `drive_auto_run_once` / `google_drive_poll_new_files`。**Railway 常駐**は `Dockerfile` + `scripts/railway_entry.sh`（`docs/spec.md`） |
| 音声〜文字起こし〜結合 | 実装済 | `run_job_once` 内 |
| 機械補正 | 実装済 | |
| **AI 補正（step_4_3）** | **実装済（2026-03 更新）** | **チャンク分割＋チャンク単位 ratio フォールバック**（全文一括廃止） |
| 不明点・質問・LINE | 実装済 | `run_question_cycle_once`、`webhook_app` |
| Webhook 起動時の Drive 常駐保険 | 実装済 | Railway 起動経路が想定と異なる場合に備え、`webhook_app.py` startup で `DRIVE_FOLDER_ID` があると Drive worker 起動を試行 |
| 再補正 | 実装済 | `recorrect_from_line_answer` 等 |
| 議事録生成・Docs export | 実装済 | `export_minutes_to_google_docs`、`compose_docs_hub_markdown`（Hub 経路） |
| **転写直後 Docs（題名 `【文字起こし】{stem}`）** | **実装済・仕様固定** | `run_job_once` の `step_4_0_transcription_docs`。**ユーザー明示指示まで当該経路は変更しない**（`docs/spec.md` 参照） |
| Docs に Q&A メタを載せない（既定） | 実装済 | `compose_docs_hub_markdown` デフォルト。`--include-internal-workspace` / `DOCS_HUB_INCLUDE_INTERNAL_WORKSPACE=1` で従来表示可 |
| Drive 自動時の重複ファイル | **対策済** | `run_pipeline` に `--no-docs-upload-source` |
| Webhook 後の E2E 自動起動 | 実装済 | `AUTO_AFTER_ANSWER` + `run_docs_hub_e2e` Popen |
| 学習（Sheets 等） | 部分済 | JSON 保存・Sheets 追記はあり。参照ループは今後 |

---

## フェーズ1：入力

- [x] Task 1-1：Google Drive 接続・一覧
- [x] Task 1-2：新規ファイル検出（差分は `last_seen_file_ids.json`）
- [x] Task 1-3：ダウンロード（`data/incoming_audio` 既定）
- **注意:** **ローカルフォルダの自動監視ではない**。必ず **Drive `--folder-id` または `DRIVE_FOLDER_ID`** 経由。詳細は **`docs/spec.md` の「Google Drive（投入〜整理の確定仕様）」**。

---

## フェーズ2：音声処理

- [x] wav 変換・チャンク分割・テスト用 `max-chunks`
- [x] txt はスキップ

---

## フェーズ3：文字起こし

- [x] Whisper チャンク・JSON 保存・結合 → `merged_transcript.txt`
- [x] **転写直後 Google Docs**（`step_4_0_transcription_docs`、Drive 題名 `【文字起こし】{stem}`、機械補正より前）
  - **確定:** 監視投入〜stem 移動〜本 Docs 作成は **`docs/spec.md` 記載のとおり固定的に扱い、勝手に変えない**（変更はユーザー指示ベース）。

---

## フェーズ4：補正

- [x] Task 4-1：チャンク結合 → `merged_transcript.txt`
- [x] Task 4-2：機械補正 → `merged_transcript_mechanical.txt`
- [x] Task 4-3：AI 補正 → `merged_transcript_ai.txt`
  - **実装内容（最新）:** `ai_correct_text.split_mechanical_for_ai_correction`（目標 5000 文字・スナップ・hard_max）＋チャンクごと `call_openai_for_correction_detailed`（`call_openai_for_correction` は薄いラッパー）＋`min_ai_length_ratio` は **チャンク主**。
- [x] Task 4-4：不明箇所抽出

**未着手・任意の改善（次の指示で検討してよいもの）**

- [ ] チャンク境界の overlap（品質が足りない場合）
- [ ] `max_output_tokens` とチャンク長のより厳密な整合
- [ ] 全体フォールバックのポリシー見直し（現状はログのみ）

---

## フェーズ5：質問ループ

- [x] 質問生成・`run_question_cycle_once`（`--send-line`）
- [x] Webhook 回答保存・`job_id` 付与
- [x] `recorrect_from_line_answer`・アンカー／スパン負荷対策
- [ ] **運用:** 回答が遅れた場合の **手動再実行**手順のドキュメント化（自動は `AUTO_AFTER_ANSWER` がカバー）

---

## フェーズ6：出力

- [x] `generate_minutes_transcript` / `generate_minutes_other_sections`
- [x] `export_minutes_to_google_docs --push`
- [x] `google_doc_hub.json`・`--update-doc-id` による同一 Docs 更新（**最終 export 時、Drive ファイル名を `stem` に付け替え**）
- [x] Drive 配置・サブフォルダ
- [x] Drive 自動時 **元ファイルの二重アップロード解消**（`--no-docs-upload-source`）
- [x] Hub 経路: **`compose_docs_hub_markdown` 既定で確認ワークスペース・回答ブロックを除外**（提出物に Q&A メタを混ぜない）
- [ ] **（未定・後段）** Docs 題名を「ステータスを消す」のではなく **次のステータス表記へ切り替える** UX（仕様・実装は未着手）

---

## フェーズ7：学習・保存

- [x] `line_answers.json`・Sheets 任意追記
- [ ] 補正時のスプレッドシート参照（未）

---

## 運用メモ（トラブル回避）

1. **同じファイル名で再投入:** stem が同じだと **relocate 先が既存と衝突**しやすい。**別ファイル名**または **残骸の整理**／`--no-relocate-input-subfolder`。
2. **`FileExistsError` on mkdir（stem）:** 同名の **ファイル** が既に存在し **フォルダを作れない**ケースあり。エクスプローラで種類を確認。
3. **`no_new_files`:** ファイルが **監視フォルダ直下**にない、**既に state に id がある**、等。
4. **Drive 自動と手動:** 手動 `run_job_once` は **ソースを Drive に upload し得る**（既定）。自動は **upload しない**。

---

## 開発ルール（継続）

1. 一気に作らず **小さくマージ**
2. **成功条件**と **ログ**で確認
3. 長時間処理は **テストモード**・**max-chunks** を活用
4. **仕様固定（2026-03-31 合意）:** **Drive 監視〜stem サブフォルダ作成・元ファイル移動・転写直後の【文字起こし】Docs（`step_4_0`）** は、**ユーザーからの具体的な指示がない限り変更しない**

---

## 変更履歴（この文書）

- **2026-03:** AI 補正チャンク化・Drive 重複対策・Webhook 自動・運用メモを反映。古い「4-3 は全文一括」の記述を削除。
- **2026-03-28:** Drive 投入仕様（直下検知・stem フォルダへ先移動）をサマリとフェーズ1に反映。
- **2026-03-29:** Railway 同居起動（Webhook + Drive ワーカー）、`DRIVE_FOLDER_ID` 等を追記。
- **2026-03-30:** `webhook_app.py` startup で Drive 常駐を起動試行する保険を追記（起動経路ズレ対策）。
- **2026-03-31:** 転写直後 Docs・題名ルール・仕様固定の開発ルール、`DRIVE_FOLDER_ID` 注記、フェーズ6の Hub compose / 将来ステータス題名（未定）、進捗サマリの追記。
