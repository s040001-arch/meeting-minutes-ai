# Meeting Minutes AI — 仕様書（最新版・2026-03）

本文書は、リポジトリ上の実装と運用上の前提を **指示役（新規チャット）向け** にまとめたものである。実装の唯一のソースはコードだが、全体像の把握に使う。

---

## 0. スクリプト・データの地図（要点）

| 領域 | 主なスクリプト | 主なデータ |
|------|----------------|------------|
| Drive 自動 1 件 | `drive_auto_run_once.py`（`drive_auto_run_forever.py` が定期起動。**本番は Railway 上で常駐**可） | 監視: `--folder-id` または **`DRIVE_FOLDER_ID`**。DL 先: `--download-dir` 既定 `data/incoming_audio`。状態: `data/last_seen_file_ids.json` |
| Railway 本番入口 | `scripts/railway_entry.sh` → Uvicorn + バックグラウンドで `drive_auto_run_forever`（起動経路が噛まない場合の保険として `webhook_app.py` 起動時にも起動試行） | 環境変数から `railway_bootstrap.py` が OAuth ファイルを生成 |
| 単発 E2E | `run_job_once.py` | `data/transcriptions/<job_id>/` 配下に中間生成物・`e2e_run_log.txt` |
| LINE Webhook | `webhook_app.py` | `data/line_answers.json`、`data/line_pending_context.json`（任意） |
| 回答後 Docs 一括 | `run_docs_hub_e2e.py` → `compose_docs_hub_markdown.py` + `export_minutes_to_google_docs.py` | `credentials.json` / `token.json`、各 job の `google_doc_hub.json` |
| AI 補正（単体） | `ai_correct_text.py` | `call_openai_for_correction_detailed` / `call_openai_for_correction`、`split_mechanical_for_ai_correction`（分割） |

**重要:** ローカル `data/incoming_audio` を **ファイルシステム監視しているわけではない**。自動投入は **Google Drive 上の指定フォルダ** を API で一覧し、新規ファイルをダウンロードしてから `run_job_once` を起動する。

### Railway 上での常時処理（PC 不要）

- **目的:** 自宅 PC を起動していなくても、**監視フォルダへの投入〜パイプライン**を実行する。
- **方式:** 同一コンテナで **HTTP Webhook**（`webhook_app` / Uvicorn）と **Drive ポーリング**（`drive_auto_run_forever.py`）を **並行起動**する。エントリポイントは `scripts/railway_entry.sh`（`Dockerfile` の `CMD`）。
- **保険（重要）:** まれに Railway 側の起動経路（Procfile/Nixpacks 等）が想定と異なり `railway_entry.sh` が効かないケースがある。このため `webhook_app.py` の FastAPI `startup` でも **`DRIVE_FOLDER_ID` が設定されている場合に `drive_auto_run_forever.py` をバックグラウンド起動**し、Drive 常駐が開始されない事態を回避する。
- **必須の環境変数（例）**
  - **`DRIVE_FOLDER_ID`**: 監視対象の Google Drive フォルダ ID（`--folder-id` と同等）。未設定なら Drive ワーカーは起動しない（Webhook のみ）。
  - **`GOOGLE_OAUTH_CLIENT_JSON`**: OAuth クライアント JSON（`credentials.json` の全文）。
  - **`GOOGLE_OAUTH_TOKEN_DRIVE_JSON`**: Drive API 用の `token_drive.json` 相当の全文（リフレッシュトークンを含む）。
  - **`GOOGLE_OAUTH_TOKEN_JSON`**: Google Docs 書き込み用の `token.json` 相当の全文（**推奨**。未設定だと Docs エクスポートが初回ブラウザ認証を要求し、サーバでは失敗しうる）。
  - **`OPENAI_API_KEY`**: AI 補正等（`run_job_once` 内で使用）。
  - その他 LINE・`AUTO_AFTER_ANSWER` 等は従来どおり。
- **起動時:** `railway_bootstrap.py` が上記 JSON 環境変数から `credentials.json` / `token_drive.json` / `token.json` を **ファイルが無いときだけ**生成する。
- **任意:** `DRIVE_POLL_INTERVAL_SEC`（既定 `120`）、`PORT`（Railway が注入）。
- **永続化:** デプロイのたびにコンテナ FS は初期化されうるため、`data/last_seen_file_ids.json`・`data/transcriptions/`・OAuth トークン更新後のファイルを保持するには **Railway の Volume を `/app/data` 等にマウント**することを推奨。
- **リソース:** 文字起こし・AI 補正は **メモリ・CPU を消費**する。常時ポーリングと合わせ **常時稼働プラン**を推奨。

---

## ① 入力・データ取得

### 入力形式
- **音声:** m4a / mp3 / wav（内部で wav に変換しチャンク分割 → Whisper）
- **テキスト:** txt（文字起こしフェーズをスキップし、内容を `merged_transcript.txt` 相当として渡す）

### Google Drive（投入〜整理の確定仕様）

- ユーザーは **監視対象フォルダの直下** にのみ、音声またはテキストファイルを置く（`--folder-id` で指定）。**サブフォルダ内に置いたファイルは新規検知の対象にしない**（Drive API では当該フォルダを親とする **直下アイテム** だけを列挙するため、移動後のファイルは一覧に出ない）。
- **新規検知の対象:** 直下にある **ファイル** のみ。直下の **フォルダ** エントリはスキップする。
- **整理（パターン1・Drive 上）:** 処理の最初に、ファイル名から **stem**（拡張子を除いた名前）を決め、監視フォルダ直下に **その stem と同一の名前のサブフォルダ** を用意し、**元ファイルをそのサブフォルダへ移動**する（`drive_auto_run_once.py` の `ensure_drive_subfolder` / `move_drive_file_to_folder`）。**移動はローカルではなく Google Drive 上**で行う。
- 続けて **同一 file_id** でローカルへダウンロードし、`run_job_once` を実行する。Docs エクスポートの親・サブフォルダ指定には、この stem を使う。
- **監視対象のフォルダ ID:** `drive_auto_run_once.py` / `drive_auto_run_forever.py` の **`--folder-id`**、または **未指定時は環境変数 `DRIVE_FOLDER_ID`**（本番・Railway ではこちらが主）。
- 対応拡張子・mime は `drive_auto_run_once.py` の `SUPPORTED_EXTENSIONS` および `select_one_new_file` を参照（txt・音声・text/plain 等）。

### job_id
- 例: `job_<YYYYMMDD_HHMMSS>_<サニタイズ済みstem>`（Drive 自動時は `build_job_id`）。

### ローカル入力の整理（`run_job_once`・手動実行時）
- **既定:** `relocate_input_into_stem_subfolder` が、入力の親ディレクトリ直下に **`<stem>` フォルダ** を作り、`…/<stem>/<元ファイル名>` へ移動する。**これはローカル作業用**であり、Drive 自動経路では **Drive 上の整理（上記）が正本**。
- **オフ:** `--no-relocate-input-subfolder`
- **既知の衝突:** 移動先に **同名ファイルが既に存在**する／**stem と同名のパスが「フォルダ」ではなく「ファイル」として存在**する／等で `RuntimeError` や `FileExistsError` になり得る。運用では **別名で再投入**、**残骸の退避**、または **relocate オフ** を検討する。

---

## ② 文字起こし（音声時）

- ffmpeg で wav、チャンク分割、`transcribe_one_chunk.py`、結合 → `merged_transcript.txt`
- txt 入力時はこのブロックをスキップ（`run_job_once` 内分岐）。

### 転写直後の Google Docs（確定仕様・2026-03-31）

**ユーザーのイメージ:** 所定の監視フォルダに音声／txt を置く → stem 名サブフォルダができ、元ファイルが **Drive 上で**そこへ移動 → **続けて**（ローカルでマージ完了後）**同一サブフォルダに**、Drive 上のファイル名が **`【文字起こし】` + stem** の Google ドキュメントが作成される。

- **実行タイミング:** `merged_transcript.txt` ができた直後、**機械補正（step_4_2）より前**（`run_job_once` 内 `step_4_0_transcription_docs`）。
- **前提条件:** `--docs-push` かつ `--docs-parent-folder-id` が指定されていること、かつ `--skip-export-docs` でないこと。`drive_auto_run_once.run_pipeline` は `--docs-push` と親フォルダ（監視フォルダ ID）を渡すため、通常は本ステップが走る。
- **題名（Drive ファイル名）:** `【文字起こし】{stem}`（`stem` は入力ファイルの拡張子を除いた名前）。
- **本文:** マージ直後の全文（Markdown 経由で Docs に投入。ジョブ内 `transcript_stage_docs.md`）。
- **同一ドキュメントの再利用:** `data/transcriptions/<job_id>/google_doc_hub.json` に `doc_id` を保存。再実行時は新規作成ではなく **既存 doc の本文更新**。
- **仕様の固定（運用合意）:** **監視フォルダへの投入 → stem サブフォルダ作成 → 元ファイルの Drive 移動 → 本ステップによる【文字起こし】Docs 作成** という流れは、**ユーザーから別途の具体的な指示がない限り変更しない**（誤って上書きしないための確定範囲）。
- **最終 export（step_6_3）との関係:** パイプライン後半で **同じ `doc_id`** に議事録用本文を差し替え。このとき **Drive 上のファイル名を stem のみ**に更新する（`--update-doc-id` 指定時に `export_minutes_to_google_docs.py` が `files.update` で名前変更）。**将来案（未定）:** 「【文字起こし】を消す」のではなく、**次のステータス表記へ付け替える**見せ方は後段機能として検討予定（本仕様では深く定義しない）。

---

## ③ 補正パイプライン（`run_job_once` 中心）

### 実行ステップ一覧（`run_job_once.py` を正本とする）

| Step | 主スクリプト / 処理 | 主入力 | 主出力 / 副作用 |
|------|----------------------|--------|------------------|
| 2.1 | `ffmpeg_convert_to_wav.py` | 元入力音声 | `data/transcriptions/<job_id>/input_16k_mono.wav` |
| 2.2 | `audio_split_chunks.py` | `input_16k_mono.wav` | `data/transcriptions/<job_id>/chunks/chunk_*.wav` |
| 3.0 | `transcribe_one_chunk.py`（チャンクごと） | `chunks/chunk_*.wav` | `data/transcriptions/<job_id>/chunk_*.json` |
| 4.0 | `run_transcription_stage_docs_export` | `merged_transcript.txt` | `data/transcriptions/<job_id>/transcript_stage_docs.md`、`data/transcriptions/<job_id>/google_doc_hub.json`、Google Docs 上の暫定 Doc `【文字起こし】{stem}` |
| 4.1 | `transcription_merge_chunks.py` | `chunk_*.json` | `data/transcriptions/<job_id>/merged_transcript.txt` |
| 4.2 | `mechanical_correct_text.py` | `merged_transcript.txt` | `data/transcriptions/<job_id>/merged_transcript_mechanical.txt` |
| 4.3 | `correct_full_text`（Claude 全文一括補正） | `merged_transcript_mechanical.txt` | `data/transcriptions/<job_id>/merged_transcript_ai.txt` |
| 4.35 | `review_risky_terms.py` | `merged_transcript_ai.txt` | `data/transcriptions/<job_id>/risky_terms.json` |
| 4.4 | `extract_unknown_points.py` | `merged_transcript_ai.txt` | `data/transcriptions/<job_id>/unknown_points.json`（直後に `risky_terms.json` をマージし、下流では混在リストを利用） |
| 5.0 | 質問候補 1 件選定 | `unknown_points.json`、文脈テキスト（優先順: `merged_transcript_after_qa.txt` → `merged_transcript_ai.txt` → `merged_transcript.txt`） | 選定済み候補と `selection_audit`（`run_question_cycle_once.py` 内部） |
| 5.1 | `run_question_cycle_once.py` 質問生成 | 上記候補 / 文脈 | `data/transcriptions/<job_id>/question_result.json` |
| 5.2 | pending context 書き出し | `question_result.json` | `data/line_pending_context.json` |
| 5.3 | LINE 用メッセージ生成 / 任意 push | `question_result.json` | `data/transcriptions/<job_id>/question_message.txt`、必要時は LINE push |
| 5.4 | `recorrect_from_line_answer.py` またはフォールバックコピー | `data/line_answers.json`、`question_result.json`、補正済み transcript | `data/transcriptions/<job_id>/merged_transcript_after_qa.txt` |
| 6.1 | `generate_minutes_transcript.py` | `merged_transcript_after_qa.txt` を優先（無ければ `merged_transcript.txt`） | `data/transcriptions/<job_id>/minutes_draft.md` |
| 6.2 | `generate_minutes_other_sections.py` | `minutes_draft.md` | `data/transcriptions/<job_id>/minutes_structured.md` |
| 6.3 | `export_minutes_to_google_docs.py` | 構造化済み議事録（+ `google_doc_hub.json`） | 同一 `doc_id` の最終更新、必要時 `google_doc_hub.json` 更新 |

### Progressive Enhancement（Google Docs）

- **Step 4.0:** `merged_transcript.txt` ができた時点で、**暫定の Google Doc** を `【文字起こし】{stem}` として作成または更新する。
- **Step 6.3:** 議事録生成完了後、**同じ `doc_id`** を最終本文で更新する。つまり「転写を早く見せる」→「後で完成版に差し替える」という Progressive Enhancement を採る。
- **txt 入力時:** Step 2.1 / 2.2 / 3.0 / 4.1 はスキップし、元 txt を `merged_transcript.txt` として扱う。

### 合流点
- 音声・txt いずれも、機械補正の入力として **`merged_transcript.txt`** を単一合流点とする（txt は入力をコピーして生成）。

### 第1段階：機械補正
- `mechanical_correct_text.py` → `merged_transcript_mechanical.txt`

### 4.3 AIテキスト補正

#### 設計思想
音声認識テキストの補正を「考える工程」と「作業する工程」に分離する。
一括で「全部直して」と投げるのではなく、段階的に処理することで
精度・速度・コストのバランスを最適化する。

#### アーキテクチャ

##### フェーズA：自動補正

| ステップ | 処理内容 | モデル | 理由 |
|----------|---------|--------|------|
| ①異常箇所の検出 | 文脈が通らない箇所をリスト化 | Claude 3.5 Sonnet | 読解力・網羅性が必要 |
| ②推測度の判定 | 各箇所の修正にどれだけ推測が必要かを0-100%で数値化 | Claude 3.5 Sonnet（①と一括実行） | 分析・判断力が必要 |
| ③自動修正の適用 | 推測度に基づきテキストを修正 | Claude 3.5 Haiku または GPT-4o-mini | 指示通りの置換作業のみ |

##### ①②の出力フォーマット（JSON）
```json
[
  {
    "location": "行番号または該当テキスト",
    "original": "元のテキスト",
    "issue": "何が問題か",
    "suggestion": "修正候補",
    "guess_level": 0-100の数値
  }
]
```

##### ③の判定ロジック
- guess_level < 10 → 自動修正を適用（ほぼ確実に正しい修正）
- guess_level >= 10 → フェーズBへ（将来実装まではそのまま残す）

##### フェーズB：人間確認ループ（将来実装）

| ステップ | 処理内容 | 備考 |
|----------|---------|------|
| ④質問の選定 | guess_level >= 10 の中から最も文脈に影響する1箇所を選ぶ | |
| ⑤LINEで質問 | LINE Messaging APIで管理者に質問を送信 | |
| ⑥回答受信 | LINE Webhookで回答を受信 | |
| ⑦回答に基づき補正 | 回答を反映してテキストを修正。未解決の箇所があれば④に戻る | |

#### API通信方式
- ①②のClaude API呼び出しはストリーミング方式（client.messages.stream）を使用
- 理由: 非ストリーミングでは24,728文字の入力で600秒ReadTimeoutが発生した実績あり
- ストリーミングにより接続を常にアクティブに保ち、タイムアウトを回避する
- タイムアウト設定: 900秒（安全弁）
- ③は入出力が小さいため非ストリーミングでも可

#### 実装優先順位
1. ストリーミング化（現在の一括方式のタイムアウト解消）
2. ①②③の3ステップ分離
3. フェーズBのLINE連携

#### マスク処理
- 固有名詞のマスク/アンマスク処理は従来通り維持
- ①②の入力時にマスク済みテキストを渡す
- ③の出力後にアンマスクする

### ハイブリッド検出レイヤー（step_4_35 + step_4_4）

- **Step 4.35 / `review_risky_terms.py`:** `merged_transcript_ai.txt` を GPT-4o でレビューし、危険語候補を **5 カテゴリ**で `risky_terms.json` に出す。
  - `organization_candidate`
  - `service_candidate`
  - `proper_noun_candidate`
  - `suspicious_number_or_role`
  - `suspicious_word`
- **出力スキーマ（現実装）:** 各要素は最低限 `type` / `text` / `reason` を持つ。監査観点では `position_ratio`（0.0–1.0）のような位置情報もあると望ましいが、**現行 `review_risky_terms.py` 実装では未付与**。
- **Step 4.4 / `extract_unknown_points.py`:** 正規表現ベースで `unknown_points.json` を生成する。カテゴリは **`固有名詞` / `数値` / `主語`**。
- **正規表現パターンの概要:**
  - `固有名詞`: `A社` / `B社` / `某社` / `〇〇` / `XX` などの曖昧な名詞
  - `数値`: `いくらか` / `金額未定` / `今月中` / `早めに` / `数件` / `何件か` / `後で調整` などの曖昧な定量・期限表現
  - `主語`: `対応する` / `更新します` などの行為表現があるのに、`担当` / `チーム` / `当社` / `私` 等の主体ヒントが無い文
- **合流:** `run_job_once.py` は Step 4.4 実行後に **`unknown_points.json` と `risky_terms.json` をマージ**し、以降の質問選定は両者の混在リストを対象にする。

### 質問選定スコアリング（step_5_0）

- **原則:** 1問で議事録の不確定性をどれだけ減らせるかを主軸にする。
- **value 式:** `value = impact - recoverability + misstatement_risk + dependency_anchor + late_document_bonus`
- **各軸:**
  - `impact`（0–13）: 型の基礎点 + 同一語の出現回数 bonus
  - `recoverability`（0–10）: 型の基礎点 + 雑談文脈 bonus。**減点軸**として使う
  - `misstatement_risk`（0–8）: 金額・期限・合意・担当などのキーワードパターン + 型 bonus
  - `dependency_anchor`（0–3）: 他の解釈の前提になりやすいか。**主語 / 数値系を優先**
  - `late_document_bonus`（0–1）: 文書後半 **52% 以降**に現れる候補を優先
- **Tiebreak:** 同点時は `(risky_band, TYPE_PRIORITY, idx)` を使う。
- **英語 / 日本語キーの統合:** `organization_candidate` / `service_candidate` / `proper_noun_candidate` / `suspicious_word` は **`固有名詞` 帯**、`suspicious_number_or_role` は **`数値` 帯**として扱う。
- **重複除去:** `(type, text)` が同一の unknown は代表 1 件に寄せ、`_dedupe_source_indexes` で元 index 群を持つ。
- **実運用:** `run_question_cycle_once.py` はまず **AI に 1 問だけ選ばせる**。AI に失敗したときのみ、`question_value_selection.py` の value-based 選定へフォールバックする。

### LINE 質問サイクル（step_5_1〜5_4）

- **1ジョブにつき最大 1 問**。`run_question_cycle_once.py` は `question_result.json` / `question_message.txt` を 1 件ぶんだけ生成する。
- **Step 5.1:** `question_result.json` を出力。`question_status` は `generated` または `none`。
- **Step 5.2:** `data/line_pending_context.json` に直近の `job_id` / `question_id` / `question_text` / `selected_unknown` / `selection_audit` を保存し、Webhook 側で回答を job に紐づける。
- **Step 5.3:** `question_message.txt` を作成し、`--send-line` 指定かつ LINE 環境変数が揃う場合のみ push する。
- **Step 5.4:** `data/line_answers.json` に回答があれば `recorrect_from_line_answer.py` で `merged_transcript_after_qa.txt` を生成する。回答が無い、空配列、または再補正失敗時は `ensure_after_qa_exists` が **`merged_transcript_ai.txt` を `merged_transcript_after_qa.txt` にコピー**して先へ進める。
- **Webhook 連携:** **LINE Webhook**（`webhook_app.py`）は回答を `data/line_answers.json` に保存する。環境変数 **`AUTO_AFTER_ANSWER`** が truthy のときのみ、保存成功後に **`run_docs_hub_e2e.py --job-id … --after-answer --push`** を **子プロセス `Popen`** で起動する（**同一 job のファイルロック**で二重起動抑制）。Webhook の HTTP 応答は失敗させない。

### Drive 自動 × 元ファイルの重複アップロード（2026-03 更新）
- **問題:** 従来、Drive 上の元ファイルを **移動**する処理と、`export_minutes_to_google_docs` の **`--upload-local-file`** が **両方**効き、案件サブフォルダに **同内容の二重ファイル**ができた。
- **対策:** `drive_auto_run_once.py` の `run_pipeline` が `run_job_once` に **`--no-docs-upload-source`** を付与。Drive 自動経路では **元ファイルは移動のみ**、**ローカルからの再アップロードは行わない**。手動 `run_job_once` の既定は従来どおり（フラグなしならアップロードし得る）。

---

## ④ 議事録生成・Google Docs

- **`run_job_once` 内（step_6）:** `generate_minutes_transcript.py` → `minutes_draft.md`、`generate_minutes_other_sections.py` → `minutes_structured.md`、`export_minutes_to_google_docs.py`（`--push`、`--update-doc-id` で同一 Doc の本文差し替え。**更新時は Drive ファイル名を `--title` に合わせる**処理あり）。
- **`run_docs_hub_e2e.py`:** Webhook 後などの compose + export オーケストレータ（`--after-answer` で再補正→`generate_minutes_transcript`→質問準備→Docs 等）。
- **`compose_docs_hub_markdown.py`:** `minutes_structured.md` を組み立て。
  - **デフォルト:** **確認ワークスペース（質問・理由・`line_answers` の回答表示など）は含めない**（提出物としての Docs に Q&A メタを混ぜない）。
  - **社内デバッグ用:** `--include-internal-workspace` または環境変数 **`DOCS_HUB_INCLUDE_INTERNAL_WORKSPACE=1`** で従来の長い Markdown を出力可能。
- **`google_doc_hub.json`:** `doc_id` 永続化。転写直後 Docs と最終 export の **同一ドキュメント**を指す。
- **再利用ルール:** Step 4.0 / Step 6.3 / `run_docs_hub_e2e.py` は、`data/transcriptions/<job_id>/google_doc_hub.json` に `doc_id` があれば **`--update-doc-id` で既存 Doc を再利用**する。**Drive 上で stem 名検索して見つける方式ではなく、ローカル meta JSON が正本**。
- **実運用上の見え方:** Step 4.0 で `【文字起こし】{stem}` の暫定 Doc を早く見せ、Step 6.3 で同じ Doc を最終議事録本文へ更新する。

---

## ⑤ 命名・ファイル配置・出力ルール（要約）

- ファイル命名の理想: `yyyy_mmdd_顧客名_会議名`（詳細は元要件どおり）。
- **入力ソースの配置:** Drive 自動では `data/incoming_audio/` にダウンロードされ、`run_job_once` の relocate が有効なら最終的に **`data/incoming_audio/<stem>/<元ファイル名>`** へ入る。
- **中間生成物 / 成果物:** 文字起こし・補正・質問・議事録生成の主出力は **`data/transcriptions/<job_id>/`** 配下に集約される（`input_16k_mono.wav` / `chunks/` / `chunk_*.json` / `merged_transcript*.txt` / `question_result.json` / `minutes_*.md` など）。
- **ワーカーログ / ロック:** 常駐 Drive ワーカーの PID ロックは **`logs/drive_auto_run_forever.lock`**。
- **ワーカー状態:** 常駐 Drive ワーカーの状態サマリは **`data/worker_status.json`**。
- **ログ:** `run_job_once` のジョブログは各 job 配下の **`e2e_run_log.txt`**、常駐系・アプリ系のログ / 補助ファイルは **`logs/`** 配下を基本とする。
- 発言録セクションは **極力原文**；フィラー削除・明らかな誤字・区切り整理のみ許可、要約・推測補完は禁止（生成スクリプトのプロンプト・ルールに依存）。

---

## ⑥ 学習・永続化（MVP）

- LINE 回答は `data/line_answers.json` に追記（**各レコードに `job_id`**）。任意で Google Sheets へも（`webhook_app`・サービスアカウント設定時）。

---

## ⑦ 運用・トラブル（短いチェックリスト）

| 現象 | 確認先 |
|------|--------|
| 自動で拾われない | ファイルは **監視フォルダ直下**か。既に **案件サブフォルダにしかない**と一覧に出ない場合がある。`last_seen_file_ids.json` で既読扱いになっていないか。 |
| `no_new_files` | 上記＋拡張子／mime。 |
| relocate 失敗 | 移動先 **同名ファイルあり**／**stem と同名のファイルがフォルダ名と衝突** 等。 |
| 議事録・Docs が二重 | Drive 自動では **`--no-docs-upload-source` 済み**か確認。手動では upload オプションの有無。 |
| AI 補正の異常 | `e2e_run_log.txt` と Drive 上の `_処理ログ_<job_id>.txt`。`step_4_3_ai_correct`、`correct_full_text:`、`[WARNING] correct_full_text:` を確認。 |

---

## 変更履歴（この文書レベル）

- **2026-03:** AI 補正のチャンク化、Drive 自動の `--no-docs-upload-source`、Webhook の `AUTO_AFTER_ANSWER`、監視が Drive 直下であることの明記、relocate 衝突の注意、スクリプト地図の追加。
- **2026-03-28:** Drive 投入を **パターン1（Drive 上で stem 名サブフォルダ作成＋元ファイル移動）** と明文化。新規検知は **監視フォルダ直下のファイルのみ**、直下フォルダエントリは除外。処理順は **先に Drive 移動 → ダウンロード → `run_job_once`**。ローカル relocate は手動経路用と注記。
- **2026-03-29:** **Railway** 上で Webhook と Drive 常駐ワーカーを同居起動（`Dockerfile` / `scripts/railway_entry.sh` / `railway_bootstrap.py`）。`DRIVE_FOLDER_ID` 等の環境変数。PC 常時起動は不要と明記。
- **2026-03-30:** `webhook_app.py` の FastAPI `startup` で Drive 常駐の起動試行を追加（起動経路のズレに対する保険）。テキスト投入でも mechanical→AI 補正→質問ループまで走ることを運用前提に追記。
- **2026-03-31:** **転写直後の Google Docs**（`step_4_0_transcription_docs`、題名 `【文字起こし】{stem}`）と仕様固定の注記。`DRIVE_FOLDER_ID` の扱いを本文に整合。**compose_docs_hub** のデフォルトから確認ワークスペース除外。最終 export 時の Drive 題名更新と「将来はステータス連続表記を検討（未定）」を注記。AI 補正の記述をチャンク＋`gpt-4.1` 系に更新。
- **2026-04-01:** `run_job_once.py` 基準の **実行ステップ 2.1→6.3 の表**、Step 4.35 + 4.4 のハイブリッド検出、Step 5.0 の value-based スコア式、LINE 質問サイクル、`google_doc_hub.json` ベースの Doc 再利用、`data/incoming_audio` / `data/transcriptions` / `logs` / `worker_status.json` の配置規約を追記。あわせて Step 4.3 の方針を **Claude 全文一括 + ストリーミング + timeout 900 秒** に更新。
