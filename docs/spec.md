# Meeting Minutes AI — spec.md (v2026-04)

---

## 0. Overview

Google Drive の固定フォルダに音声ファイルまたはテキストファイルをアップロードすると、自動で文字起こし・AI補正・不明点検出・議事録生成を行い、LINEを通じてユーザーと対話しながら議事録を完成させるシステム。

Railway 上で稼働し、Google Drive / Google Docs / LINE Messaging API / Claude 3.5 Sonnet / OpenAI Whisper を利用する。

---

## 1. ファイル命名規則

```
YYYY_MMDD_顧客名_会議種別.{拡張子}
```

例: `2025_0610_ABC商事_定例会議.m4a`

対応拡張子:
- 音声: `.m4a`, `.mp3`, `.wav`, `.ogg`, `.webm`
- テキスト: `.txt`

---

## 2. Google Drive フォルダ構成

```
固定フォルダ (DRIVE_FOLDER_ID)/
├── 2025_0610_ABC商事_定例会議.m4a   ← 新規アップロード（ルート直置き）
├── 2025_0610_ABC商事_定例会議/       ← 処理開始後に自動作成されるサブフォルダ
│   ├── 2025_0610_ABC商事_定例会議.m4a（移動済み元ファイル）
│   ├── merged_transcript.txt
│   ├── correction_dict.json
│   ├── unknown_points.json
│   └── Google Doc（議事録）
└── 2025_0605_XYZ工業_キックオフ/     ← 過去ジョブ
```

---

## 3. Google Doc 命名規則（ステータスベース）

処理の進行に応じて Google Doc のタイトルを変更する。

| タイミング | Doc タイトル | 意味 |
|---------|-------------|------|
| 転写直後・Doc初期作成 | `【文字起こし】2025_0610_ABC商事_定例会議` | 生の文字起こしを確認できる初期状態 |
| Step⑦ 機械補正完了 | `【機械補正完了】2025_0610_ABC商事_定例会議` | 辞書ベース置換完了 |
| Step⑧⑨ AI補正＋不明点検出完了 | `【AI補正完了】2025_0610_ABC商事_定例会議` | Claude による補正フェーズ完了 |
| Step⑩ ハイブリッド検出完了 | `【不明点検出完了】2025_0610_ABC商事_定例会議` | 不明点候補の抽出完了 |
| Step⑪ 議事録生成・Doc上書き完了 | `2025_0610_ABC商事_定例会議` | Fix 可能状態（プレフィックスなし） |
| LINE質問送信（Step⑫⑬） | `2025_0610_ABC商事_定例会議` | ユーザー確認待ちでもタイトルは維持 |
| LINE回答でループ再開 | `【機械補正完了】2025_0610_ABC商事_定例会議` | 再処理ループの再開点 |
| 次ファイル投入 | 前ジョブはその時点のタイトルのまま確定 | 新ジョブ開始に伴い前ジョブを終了 |

実装上は進捗可視化のため、`【AI補正中：マスキング】` や `【AI補正中：AI検出】` のような細粒度タイトルを一時的なサブステータスとして使用してよい。

---

## 4. Google Doc フォーマット仕様

- **フォント**: Arial 11pt（全体）
- **見出し**: Google Docs の Heading 1 / Heading 2 を使用（サイドバー目次に自動表示される）

### セクション構成

| # | セクション名 | スタイル | 内容 |
|---|------------|---------|------|
| 1 | {タイトル} | Heading 1 | ファイル名から推定した会議タイトル |
| 2 | 参加者 | Heading 2 | 発言者から推定、箇条書き |
| 3 | 議題 | Heading 2 | 話されたトピック名のみ、箇条書き |
| 4 | 決定事項 | Heading 2 | 箇条書き |
| 5 | 残論点 | Heading 2 | 箇条書き |
| 6 | Next Action | Heading 2 | 箇条書き |
| 7 | 発言録（逐語） | Heading 2 | 話者ラベルなし、意味の区切りで改行 |

---

## 5. ワークフロー

### Phase A: 初期処理（Step①〜⑥）

```
Step① ポーリングで Google Drive ルート直下の新規ファイルを検出
Step② サブフォルダを作成（ファイル名の拡張子なし）
Step③ ファイルをサブフォルダに移動
Step④ 音声ファイルの場合 → Whisper で文字起こし（チャンク分割対応）
       テキストファイルの場合 → そのまま使用
       ※ filename_hints.py により、ファイル名から顧客名・会議種別を抽出し、
         Whisper の initial_prompt および後続 AI 処理のコンテキストとして使用する
Step⑤ merged_transcript.txt をサブフォルダに保存
Step⑥ Google Doc を作成、タイトル「【文字起こし】{stem}」、
       merged_transcript.txt の内容を書き込み
```

### Phase B: 補正ループ（Step⑦〜⑰）

```
Step⑦  機械補正
        correction_dict.json の全エントリを merged_transcript.txt に適用
        → Google Doc を上書き
        → タイトルを「【機械補正完了】{stem}」に変更

Step⑧  AI 補正（Claude 3.5 Sonnet）
        機械補正済みテキストに対し、推論ベースの誤変換・誤認識修正を実行
        → Google Doc を上書き

Step⑨  AI 不明点検出（Claude 3.5 Sonnet）
        Step⑧と同時または直後に実行
        Claude が検出した不明箇所を unknown_points.json に出力
        → タイトルを「【AI補正完了】{stem}」に変更

Step⑩  ハイブリッド不明点検出
        Regex ベースの検出を実行し、Claude の検出結果とマージ
        → unknown_points.json を更新
        → タイトルを「【不明点検出完了】{stem}」に変更

Step⑪  議事録生成（Claude 3.5 Sonnet）
        Google Doc を全面上書きし、セクション4のフォーマットで議事録を生成
        → タイトルを「{stem}」に変更（プレフィックスなし＝確定可能状態）

Step⑫  質問選定
        unknown_points.json から未回答の不明点を1つ選定
        選定基準: 議事録の正確性に最もインパクトが大きいもの
        不明点がゼロの場合 → Step⑬へ（完了通知）

Step⑬  LINE 通知
        不明点あり → 質問を送信（後述のフォーマット）
        不明点なし → 完了通知を送信
        → Step⑭へ

Step⑭  Suspend（非ブロッキング待機）
        LINE 応答またはポーリングでの新規ファイル検出を待つ
        ブロッキングしない（ポーリングループは継続）

Step⑮  Resume トリガー
        パターンA: LINE メッセージ受信 → Step⑯へ
        パターンB: 新規ファイル検出 → 現ジョブを確定終了、新ジョブの Step① へ

Step⑯  LINE メッセージ分類（Claude 3.5 Sonnet）
        受信メッセージを AI で3分類:
        1. 質問への回答 → unknown_points.json の該当エントリを更新 → Step⑦へ
        2. 修正依頼 → AI で辞書エントリを生成、correction_dict.json に追記 → Step⑦へ
        3. 無関係 → 無視、Step⑭に戻る

Step⑰  ループ継続
        Step⑦に戻り、補正済み merged_transcript.txt から再処理
        ただし回答済みの不明点は除外
```

---

## 6. Regex パターン（Step⑩ ハイブリッド検出）

7種類のパターンでテキストをスキャンし、不明点候補を抽出する。

### Pattern 1: 固有名詞の表記揺れ

同一の固有名詞が複数の表記で出現するケースを検出。

```
例: 「TOKIO」と「高尾」が混在
例: 「thr」「dhr」「phr」の混在
例: 「秋元」と「秋本」の混在
```

実装: 音が近い文字列のクラスタリング（編集距離 or 読み仮名ベース）

### Pattern 2: 漢字変換エラー

文脈上ありえない漢字の組み合わせを検出。

```
例: 「人的尊敬」→「人的資本経営」
例: 「三菱鑑賞会社」→「三菱商事会社」
例: 「既存の改革」→「既存の概念」
```

実装: 既知の誤変換パターン辞書 + 共起頻度の低い漢字列の検出

### Pattern 3: 数値の矛盾・異常

文脈に対して桁や単位が不自然な数値を検出。

```
例: 「52.5万」→ 文脈から「5万2500」の可能性
例: 「23年前」→ 文脈から「2〜3年前」の可能性
例: 同一の数値が異なる表記で出現
```

実装: 数値抽出 + 前後文脈との整合性チェック

### Pattern 4: 短い無意味語（2〜4文字）

文脈に合わない短い単語を検出。

```
例: 「ネギ目」「ポキャブ」「メヒモ」
```

実装: 形態素解析で未知語判定 + 文字数フィルタ

### Pattern 5: Whisper 特有マーカー

Whisper が出力する特徴的なアーティファクトを検出。

```
例: 「ご視聴ありがとうございました」（音声末尾の幻聴）
例: 同一フレーズの連続繰り返し
例: 「♪」「...」の異常な連続
```

### Pattern 6: フィラー・言い淀みの塊

発言として意味をなさないフィラーの塊を検出。

```
例: 「えーっと、あの、その、えー、まあ」が連続
```

### Pattern 7: 括弧・記号の不整合

閉じ括弧の欠落や記号の異常使用を検出。

```
例: 開き括弧に対応する閉じ括弧がない
例: 「？」「！」の文脈に合わない出現
```

---

## 7. LINE 通知フォーマット

### 質問通知（不明点あり）

```
📝 2025_0610_ABC商事_定例会議
https://docs.google.com/document/d/xxxxx

「52.5万」という記載がありますが、文脈から「5万2500」のことだと思われます。合っていますか？
```

- Google Doc の URL を必ず含める
- 質問は端的に自然語で記述
- 仮説を提示し「合っていますか？」形式にする（Yes で済むようにする）

### 完了通知（不明点なし）

```
✅ 2025_0610_ABC商事_定例会議
https://docs.google.com/document/d/xxxxx

不明点の確認がすべて完了しました。議事録が確定状態です。
```

---

## 8. LINE メッセージ分類（Step⑯）

受信した LINE メッセージを Claude 3.5 Sonnet で3分類する。

| 分類 | 説明 | 後続処理 |
|------|------|---------|
| 1. 質問への回答 | 直前の質問に対する回答（Yes/No、訂正値など） | unknown_points.json 更新 → Step⑦ |
| 2. 修正依頼 | 「〇〇は△△の間違い」等のフリーテキスト修正指示 | AI で辞書エントリ抽出 → correction_dict.json 追記 → Step⑦ |
| 3. 無関係 | 会議議事録と関係ないメッセージ | 無視 → Step⑭ に戻る |

### 修正依頼の辞書エントリ生成

ユーザーの自由文から Claude が `{誤: 正}` のペアを抽出し、correction_dict.json に追記する。

```
ユーザー: 「高尾じゃなくてTAKIOです」
→ {"高尾": "TAKIO"} を辞書に追加
```

---

## 9. ジョブ管理

### 同時実行

- アクティブなジョブは常に1つのみ
- 新規ファイルがルートにアップロードされた時点で、前のジョブは「確定終了」

### 確定終了時の処理

- 前ジョブの Google Doc タイトルが既に `{stem}`（プレフィックスなし）であればそのまま
- プレフィックス付きの場合も、その時点の状態で確定（追加処理なし）

### Suspend / Resume

- Step⑭ の Suspend は**非ブロッキング**
- メインのポーリングループ（Google Drive 監視）は継続稼働
- LINE Webhook 受信時に Resume をトリガー

### 状態管理（progress_tracker.py）

`progress_tracker.py` は進捗表示専用のモジュールとし、ログ出力・可視化のための状態を扱う。

管理対象の例:
- 現在のステップ番号
- ステップごとの進行状況
- ジョブの開始時刻

状態制御そのものは `job_state.py` に分離する方針とする。`job_state.py` は Phase 2 で新設予定であり、Suspend / Resume や再開制御の責務を持つ。

### デバッグログ（Drive visible logs）

処理の進行状況を Google Drive のサブフォルダ内にテキストファイルとして出力する。
Railway のログが流れた後でも、Drive 上で処理状況を確認できるようにする目的。

---

## 10. 永続化（Railway Volume）

マウントポイント: `/app/data`

```
/app/data/
├── last_seen_file_ids.json    ← ポーリング用の既知ファイルID一覧
├── correction_dict.json       ← グローバル補正辞書（ジョブ横断で蓄積）
└── jobs/
    └── 2025_0610_ABC商事_定例会議/
        ├── merged_transcript.txt   ← オリジナルの文字起こし（不変）
        ├── unknown_points.json     ← 不明点リスト（回答済みフラグ付き）
        └── job_state.json          ← 現在のステップ、Doc ID 等
```

### correction_dict.json

- ジョブをまたいで蓄積される
- 手動修正依頼から生成されたエントリも含む
- Step⑦ で毎回全エントリを適用

### unknown_points.json

```json
[
  {
    "id": "up_001",
    "source": "claude",
    "text": "52.5万",
    "context": "〜〜で52.5万くらいの規模感で〜〜",
    "hypothesis": "5万2500のことだと思われる",
    "status": "open",
    "answer": null
  },
  {
    "id": "up_002",
    "source": "regex_pattern3",
    "text": "23年前",
    "context": "〜〜を23年前に導入して〜〜",
    "hypothesis": "2〜3年前の可能性",
    "status": "answered",
    "answer": "2〜3年前が正しい"
  }
]
```

### google_doc_hub.json

同一 `job_id` のジョブディレクトリに紐づく Google Doc の ID や関連メタデータを記録する。ジョブ実行中に同じ `job_id` で再出力が発生した場合、新規作成せず既存 Doc を上書き再利用する。

```json
{
  "2025_0610_ABC商事_定例会議": "1aBcDeFgHiJkLmNoPqRsTuVwXyZ"
}
```

保存先: `data/transcriptions/<job_id>/google_doc_hub.json`

---

## 11. 技術スタック

| コンポーネント | 技術 |
|--------------|------|
| ホスティング | Railway |
| 永続化 | Railway Volume (`/app/data`) |
| 音声文字起こし | OpenAI Whisper API |
| AI 補正・検出・分類・議事録生成 | Claude 3.5 Sonnet (Anthropic API) |
| ファイル監視 | Google Drive API v3（ポーリング） |
| ドキュメント生成 | Google Docs API v1 |
| ユーザー通知・対話 | LINE Messaging API |
| OAuth 管理 | Google OAuth 2.0（環境変数から JSON 復元） |

---

## 12. 環境変数

```
GOOGLE_OAUTH_CLIENT_JSON       ← OAuth クライアント JSON（環境変数から復元）
GOOGLE_OAUTH_TOKEN_JSON        ← OAuth トークン JSON（環境変数から復元）
GOOGLE_OAUTH_TOKEN_DRIVE_JSON  ← Google Drive 用 OAuth トークン JSON（環境変数から復元）
GOOGLE_DRIVE_CREDENTIALS_PATH  ← 復元済み OAuth クレデンシャルファイルのパス
GOOGLE_DRIVE_TOKEN_PATH        ← 復元済み OAuth トークンファイルのパス
DRIVE_FOLDER_ID                ← Google Drive の監視対象フォルダ ID
ANTHROPIC_API_KEY              ← Claude API キー
OPENAI_API_KEY                 ← OpenAI API キー（Whisper + その他 OpenAI 系機能で共用）
LINE_CHANNEL_ACCESS_TOKEN      ← LINE Messaging API アクセストークン
LINE_USER_ID                   ← 通知先の LINE ユーザー ID
```

---

## 13. エントリポイント

```
railway_entry.sh
  → railway_bootstrap.py（環境変数から OAuth JSON ファイルを復元）
  → drive_auto_run_forever.py（メインループ起動）
```

### メインループの動作

```python
while True:
    # 1. Google Drive ルートをポーリング
    new_file = check_for_new_file()

    if new_file:
        # 前ジョブがあれば確定終了
        finalize_previous_job()
        # 新ジョブ開始 (Phase A → Phase B)
        start_new_job(new_file)

    # 2. アクティブジョブが Suspend 中なら LINE 受信をチェック
    if active_job and active_job.is_suspended():
        message = check_line_messages()
        if message:
            handle_line_message(message)  # Step⑯ の分類処理

    sleep(POLLING_INTERVAL)
```
