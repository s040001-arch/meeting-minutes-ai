# Meeting Minutes AI — spec.md (v2026-04)

---

## 0. Overview

Google Drive の固定フォルダに音声ファイルまたはテキストファイルをアップロードすると、自動で文字起こし・AI補正・不明点検出・議事録生成を行い、LINEを通じてユーザーと対話しながら議事録を完成させるシステム。

Railway 上で稼働し、Google Drive / Google Docs / Google Sheets / LINE Messaging API / Claude 4 Opus / OpenAI Whisper を利用する。

**品質目標:** AI補正による誤変換修正率 85% 以上（現状 45〜50%）

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

実装上は進捗可視化のため、`【AI補正中】` のような一時的なサブステータスをタイトルに使用してよい。

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
        助詞重複・連続短句・明らかな言い直し残りなど、意味を変えない範囲の機械整形も行う
        → Google Doc を上書き
        → タイトルを「【機械補正完了】{stem}」に変更

Step⑧  AI 補正（Claude 4 Opus）
        機械補正済みテキストを Claude 4 Opus に一括で渡し、推論ベースの誤変換・誤認識修正を実行する。
        マスキング等の多段処理は行わず、テキスト全体をそのまま渡すことで文脈を保持する。

        プロンプトに含めるコンテキスト情報:
        - 参加者メタデータ（名前・役職・担当、context.json から注入）
        - 企業名・サービス名の正式名称リスト（context.json および ナレッジメモから注入）
        - 業界用語辞書（Google Sheets ナレッジメモ全件）
        - ファイル名ヒント（顧客名・会議種別）
        - 補正ルール（表記統一・フィラー除去・意味不明箇所の推測補正）

        Google Sheets に蓄積されたナレッジメモ全件をプロンプトに含める。
        → Google Doc を上書き

Step⑨  AI 不明点検出（Claude 4 Opus）
        Step⑧と同時または直後に実行
        Claude が検出した不明箇所を unknown_points.json に出力
        → タイトルを「【AI補正完了】{stem}」に変更

        重複質問防止のために以下を参照する:
        - Google Sheets ナレッジシート: 過去のジョブで蓄積された知識（別ジョブの Q&A から得た固有名詞・背景情報等）
        - answers.json（または unknown_points.json の answered 項目）: 現在のジョブ内で既に得られた回答
        両者に記載されている情報については、不明点として検出・質問しないこと

Step⑩  ハイブリッド不明点検出
        Regex ベースの検出を実行し、Claude の検出結果とマージ
        → unknown_points.json を更新
        → タイトルを「【不明点検出完了】{stem}」に変更

Step⑪  議事録生成（Claude 4 Opus）
        Google Doc を全面上書きし、セクション4のフォーマットで議事録を生成
        → タイトルを「{stem}」に変更（プレフィックスなし＝確定可能状態）
        回答反映後の再開ループでも、最終的にタイトルはプレフィックスなしへ戻す

Step⑰  ナレッジ蓄積（Step⑪完了直後に実行）
        目的: LINE 回答で得られた知識を永続化し、将来の別ジョブで同じ質問を繰り返さないようにする
        answers.json（現在のジョブで得られた回答の蓄積）を Knowledge Sheet に反映する
        処理方式:
        - Knowledge Sheet の全エントリを読み込み、今回の回答群とともに Claude Opus 4 に渡す
        - Claude が重複・統合・更新を判断し、整理済みの全エントリを書き戻す（単純追記ではなく、
          既存ナレッジとの整合を取った最適状態に再編成する）
        この処理は次回以降のジョブのためであり、現在のジョブの補正には影響しない

Step⑫  質問選定
        unknown_points.json から未回答の不明点を1つ選定
        選定基準: 単語単位の確認に限らず、逐語録全体の文意を最も回復できる論点を優先する
        少しでも不安が残る場合は質問するが、優先順位は「全体インパクト」を最上位とする
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

Step⑯  LINE メッセージ情報抽出（Claude 4 Opus）
        受信メッセージを「回答か修正依頼か」に厳密分類するのではなく、
        議事録補正に使える情報を抽出する:
        1. 本文へ反映できる回答情報
        2. correction_dict.json に入れられる置換ペア
        3. どちらも取れない場合のみ無関係
        回答情報と修正依頼が1通に同居している場合は、両方とも反映してよい
        回答が含まれる場合は受信時点で unknown_points.json の該当エントリを回答済みに更新し、
        answers.json にも追記する
        → Step⑦に戻り、補正済み merged_transcript.txt から再処理（ループ継続）
          回答済みの不明点、および回答により実質解決したと LLM が判断した不明点は除外
```

---

## 5-A. LINE webhook 処理フロー

### 非同期処理の方針

Claude 4 Opus の処理時間は 4〜5 分を想定する。LINE の reply token（30 秒失効）では応答できないため、以下の構成とする。

**変更後の処理フロー:**
```
LINE webhook 受信
  → /callback が即座に HTTP 200 を返す
  → FastAPI BackgroundTasks で handle_user_input() を非同期実行
    → Claude 呼び出し（メッセージ分類・ナレッジ蓄積）
    → subprocess.Popen で run_resume_from_step7.py 起動（ノンブロッキング）
  → 処理完了後、push message API でユーザーに通知
```

### reply API → push message API への変更

| 項目 | 変更前 | 変更後 |
|------|-------|-------|
| 応答方式 | LINE Reply API（replyToken 使用） | LINE Push Message API |
| タイムアウト | reply token 30 秒失効 | 制約なし |
| 必要パラメータ | replyToken（webhook イベントから取得） | LINE_USER_ID（環境変数で設定済み） |

`LINE_USER_ID` 環境変数は既に設定済みであり、`run_job_once.py` 内の push 送信でも利用されている。

### 音声処理パイプラインは変更不要

`drive_auto_run_forever.py → subprocess.Popen → run_job_once.py` の構成は既にノンブロッキングであり、Railway の HTTP タイムアウト（300 秒）の影響を受けない。変更不要。

### Railway タイムアウト整理

| 処理 | 方式 | タイムアウト影響 |
|------|-----|--------------|
| 音声処理パイプライン（Whisper → 補正 → Doc 生成） | subprocess.Popen（非同期） | 影響なし |
| LINE メッセージ分類（Claude Opus 呼び出し） | BackgroundTasks（非同期化後） | 影響なし |
| /callback HTTP レスポンス | 即座に 200 返却 | 問題なし |

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
- 単語確認に限らず、「この1問で全体文脈がつながる」なら大きめの質問を優先してよい
- 仮説を提示する形式は有効だが必須ではない。必要に応じて「どの会社とどの会社が何を目的として議論しているか？」のような文脈確認質問も許容する

### 完了通知（不明点なし）

```
✅ 2025_0610_ABC商事_定例会議
https://docs.google.com/document/d/xxxxx

不明点の確認がすべて完了しました。議事録が確定状態です。
```

---

## 8. LINE メッセージ情報抽出（Step⑯）

受信した LINE メッセージから、Claude 4 Opus が補正に使える情報を抽出する。

| 抽出対象 | 説明 | 後続処理 |
|------|------|---------|
| 1. 回答情報 | 直前の質問に対する返答として本文へ反映できる情報 | unknown_points.json を回答済みに更新し、本文反映へ回す |
| 2. 修正依頼 | 「〇〇は△△の間違い」等の置換指示 | correction_dict.json へ追記し、Step⑦ の機械補正から反映 |
| 3. 無関係 | 会議議事録補正に使える情報が取れないメッセージ | 無視 → Step⑭ に戻る |

回答情報と修正依頼は排他的ではなく、1通のメッセージから両方抽出して同時に反映してよい。

### 修正依頼の辞書エントリ生成

ユーザーの自由文から Claude が `{誤: 正}` のペアを抽出し、correction_dict.json に追記する。

```
ユーザー: 「高尾じゃなくてTAKIOです」
→ {"高尾": "TAKIO"} を辞書に追加
```

### 回答由来ナレッジの蓄積

- LINE の回答から得られた知識は、ジョブをまたいで再利用できるよう Google Sheets（ナレッジシート）に永続化する
- 蓄積処理は **Step⑰** で実行する（Step⑯ で受信・記録し、Step⑪→⑰ のタイミングで Knowledge Sheet へ反映）
- 保存形式: A列に自由記述テキスト（1行1エントリ）、B列にカテゴリ
- 例: `DCS（Dedicated Client Service）とは営業の顧客単位の責任者のこと`
- correction_dict.json は機械補正用の置換辞書、Google Sheets 側は AI補正・不明点検出用の参考知識として役割を分離する
- ユーザーがスプレッドシートを直接編集して追記・修正してよい
- Claude には既存ナレッジ全件・今回の質問文・今回の回答文を渡し、蓄積価値の判定、重複整理、表現統合を任せる
- アプリ側は Claude が返した更新後ナレッジ全体でスプレッドシートを丸ごと書き換える（単純追記ではない）

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

ローカル/Volume 側にも `processing_visible_log.txt` を出力し、
各 Step の開始・完了・エラーが一目で分かる要約ログとして扱う。

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
        ├── job_state.json          ← 現在のステップ、Doc ID 等
        └── context.json            ← ジョブ固有コンテキスト（任意配置）
```

### context.json（ジョブ固有コンテキスト）

ジョブディレクトリに `context.json` を配置すると、Step⑧ の Opus 補正プロンプトにコンテキスト情報が自動注入される。全フィールドは任意。

```json
{
  "participants":      ["相原 隆太郎", "高橋季央", "松本侑磨"],
  "related_companies": ["THR", "デイシス", "矢崎グループ"],
  "agenda":            ["仕事力サーベイのTHR展開", "グループ会社への展開戦略"],
  "notes":             "その他の補足情報"
}
```

### correction_dict.json

- ジョブをまたいで蓄積される
- 手動修正依頼から生成されたエントリも含む
- Step⑦ で毎回全エントリを適用

### Google Sheets ナレッジシート

シート構造:
- **A列**: 自由記述のナレッジ（1行1エントリ）。単語の定義に限らず、目的・背景・因果関係など文脈を含む記述も対象とする
- **B列**: カテゴリ（人名 / 組織 / 略称 / 背景 / 因果関係 等）

運用ルール:
- correction_dict.json とは別管理とし、置換辞書ではなく AI補正・不明点検出用の参考知識として扱う
- **参照**: Step⑧（AI補正）と Step⑨（AI不明点検出）の両方でプロンプトに全件を注入する
- **更新**: Step⑰（ナレッジ蓄積）で回答内容を整理・統合してスプレッドシートに書き戻す
- ユーザーの直接編集を許容し、アプリは最新状態を都度読み込む
- アプリ側は Claude が返した更新後ナレッジ全体でシートを丸ごと書き換える（単純追記ではない）

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

- `status` は `open` / `answered` を基本とする
- `answered` への更新タイミングは「LINE の回答を受信した時点」とする
- 回答後、LLM がその回答で周辺の不明点も実質解決したと判断した場合は、追加の質問対象から外してよい
- どの項目を周辺解決扱いにしたかの内部監査ログは必須としない

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
| AI 補正・検出・分類・議事録生成 | Claude 4 Opus (Anthropic API) |
| ファイル監視 | Google Drive API v3（ポーリング） |
| ドキュメント生成 | Google Docs API v1 |
| ナレッジ蓄積 | Google Sheets API |
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
KNOWLEDGE_SHEET_ID             ← ナレッジ蓄積用 Google Sheets のスプレッドシート ID
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
