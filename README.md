# meeting-minutes-ai

議事録AIのクラウド常駐 Python システムです。  
Pixel で録音した m4a 音声を起点に、Whisper API で文字起こしし、OpenAI GPT で前処理、Claude で議事録生成を行い、Google Drive / Docs へ保存し、LINE公式アカウントから通知とQ&Aを提供します。

---

# システム概要

会議音声から議事録を自動生成し、Google Docsへ保存し、LINEから参照・質問できるシステムです。

主な機能

- 音声 → 文字起こし
- 文字起こし → 議事録生成
- Google Docs 自動更新
- LINE通知
- LINE議事録Q&A
- 辞書優先表記
- 話者ラベル付与
- 発言順序検証
- 段階別検証警告ログ出力

---

# 技術スタック

- Python
- Whisper API
- OpenAI GPT
- Claude
- Google Drive API
- Google Docs API
- Google Sheets API
- LINE Messaging API

---

# ディレクトリ構成

```text
meeting-minutes-ai
├ README.md
├ requirements.txt
├ .env.example
├ config/settings.py
├ main.py
├ audio/audio_loader.py
├ transcription/whisper_transcribe.py
├ preprocess/transcript_cleaner.py
├ preprocess/transcript_preprocessor_gpt.py
├ minutes/minutes_generator_claude.py
├ minutes/minutes_formatter.py
├ docs/google_docs_writer.py
├ dictionary/company_dictionary.py
├ dictionary/abbreviation_dictionary.py
├ pipeline/meeting_pipeline.py
├ utils/file_parser.py
├ utils/logger.py
└ line/line_webhook_handler.py
```

---

# 音声ファイル仕様

## ファイル形式

```text
m4a
```

Pixel録音を想定。

---

# 音声ファイル命名ルール

```text
YYYY_MMDD_顧客名_会議タイトル.m4a
```

例

```text
2025_0214_株式会社サンプル_定例会議.m4a
```

---

## file_parser による検証

`utils/file_parser.py` が以下を実施

- ファイル名構造検証
- 会議日抽出
- 顧客名抽出
- 会議タイトル抽出

### 不正ファイル名

命名ルール違反の場合

```text
パイプラインを即停止
```

以下は実行されない

- Whisper
- GPT前処理
- Claude生成
- Google Docs更新
- LINE通知

---

# 音声文字起こし

使用

```text
Whisper API
```

## 日本語固定指定

`transcription/whisper_transcribe.py`

Whisperのlanguageを固定

```text
language="ja"
```

目的

- 日本語認識精度安定
- 誤言語判定防止

---

# Whisperチャンク処理

- 大容量音声はチャンク分割
- 各チャンクをWhisperで処理
- チャンク結果をタイムスタンプ順にソート
- その後にテキスト統合

これにより

```text
発言順序の崩れを防止
```

---

# 文字起こし整形

```text
preprocess/transcript_cleaner.py
```

役割

- 発言単位を維持
- ノイズ除去
- 空白整理

### 順序保証

cleanerは

```text
発言順序を変更しない
```

さらに

- 発言件数検証
- 並び順検証

を実施。

異常があれば logger に警告出力。

---

# GPT前処理

```text
preprocess/transcript_preprocessor_gpt.py
```

役割

- 誤字修正
- 発言単位維持
- 辞書反映
- 話者推定

### 順序保証

GPT出力は以下を検証

- 発言件数一致
- 発言順序一致
- 発言欠落なし

異常が検知された場合

```text
logger.warning
```

---

# 話者ラベル付与

GPT前処理で仮話者ラベル付与

ラベル

```text
顧客
プレセナ
```

---

## 設定

```text
config/settings.py
```

例

```text
ENABLE_SPEAKER_LABELING=True
SPEAKER_LABEL_CONFIDENCE_THRESHOLD=0.7
```

---

# 辞書連携

Google Sheets辞書

## 種類

- 企業名辞書
- 略語辞書

---

## 使用箇所

- GPT前処理
- Claude議事録生成

---

## フォールバック

Sheets取得失敗

↓

```text
ローカル辞書
```

---

# Claude議事録生成

```text
minutes/minutes_generator_claude.py
```

Claudeは

```text
JSON議事録
```

を生成。

---

## 発言録生成

Claudeは

GPT前処理の発言単位を

```text
そのまま維持
```

して発言録を生成。

### 検証

以下を実施

- 発言件数一致
- 発言順序一致

異常時

```text
logger.warning
```

---

# 議事録構造

```text
会議概要
決まったこと
残論点
Next Action
発言録
```

---

# minutes_formatter

```text
minutes/minutes_formatter.py
```

役割

- セクション整形
- 表記統一

### 発言録検証

formatterでは

- 発言件数保持
- 順序保持

を検証。

異常があれば

```text
logger.warning
```

---

# 発言順序検証（Pipeline）

`pipeline/meeting_pipeline.py` では  
以下の各段階で発言件数と順序を検証します。

```text
Whisper
↓
cleaner
↓
GPT前処理
↓
Claude
↓
formatter
```

各段階で

- 発言件数
- 発言順序
- 発言欠落

を検証。

---

## 検証ログ

各ステージ結果は pipeline 内で集約されます。

例

```text
whisper
cleaner
gpt_preprocess
claude_record
formatter_record
```

---

## 警告ログ統一仕様

発言件数・順序検証の警告は `utils/logger.py` の専用ログ関数で出力します。

専用関数

```text
log_utterance_validation_warning(...)
```

この関数により、各段階の検証異常は

- 段階名付き
- 入力件数付き
- 出力件数付き
- issues 付き

の**統一フォーマット**で `logger.warning` 出力されます。

### 統一フォーマットの考え方

- すべての検証警告の出力形式を共通化
- pipeline から個別の warning 文言を直接ばらばらに出さない
- 段階名ごとの異常を追跡しやすくする
- ログ解析しやすい形式に統一する

### 出力対象

以下の各段階で異常があれば、専用ログ関数を通じて警告出力されます。

```text
cleaner
gpt_preprocess
claude_record
formatter_record
```

必要に応じて pipeline 集約警告もあわせて出力します。

### 想定出力形式

```text
[UTTERANCE_VALIDATION_WARNING] stage=<段階名> input_count=<件数> output_count=<件数> issues=<異常内容>
```

---

## 異常検知

いずれかの段階で

- 発言件数不一致
- 順序変更
- 発言欠落

が検出された場合

pipelineは `utils/logger.py` の専用ログ関数を使って、段階名付きの統一フォーマットで警告を出力します。

システムは停止せず処理を継続します。

---

# Google Drive

会議ごとにフォルダ作成

```text
YYYY_MMDD_顧客名_会議タイトル
```

---

## 保存物

- 音声
- 議事録Docs

---

## 音声アップロード

アップロード前に

```text
同名音声存在確認
```

存在する場合

```text
アップロードスキップ
```

---

# Google Docs

同名Docs

```text
再利用
```

---

## 更新仕様

Docs更新時

```text
既存内容削除
↓
新議事録書き込み
```

---

# LINE連携

LINE公式アカウント使用

---

# LINE Webhook

Webhook受信時

```text
署名検証
```

---

## 署名検証

成功

```text
LINE処理実行
```

失敗

```text
処理拒否
```

---

# 内部状態管理

議事録生成完了時

内部状態ファイル更新

---

## 保存内容

- Google Docs URL
- 会議情報
- 議事録本文

---

# LINE Q&A

参照対象

```text
最新議事録1件
```

---

## 内部状態異常

以下の場合

- 未保存
- 欠損
- 破損

LINE返信

```text
まだ最新議事録を参照できません
```

システムは停止しない。

---

# ログ

```text
logs/meeting.log
processed_audio.log
```

出力

- stdout
- ファイル

---

# 起動

```text
main.py
```

---

# 処理フロー

```text
音声取得
↓
file_parser検証
↓
Driveフォルダ確認
↓
音声アップロード
↓
Whisper(language=ja)
↓
cleaner
↓
GPT前処理
↓
Claude議事録生成
↓
formatter
↓
発言順序検証集約
↓
Google Docs上書き
↓
内部状態保存
↓
LINE通知
↓
LINE Q&A
```

---

# 実装済み機能

- Whisperチャンク分割
- Whisper日本語固定指定
- Whisperチャンク順序統合
- cleaner順序検証
- GPT前処理順序検証
- Claude発言録順序検証
- formatter順序検証
- pipeline順序検証集約
- 専用ログ関数による段階名付き統一警告出力
- GPT前処理
- Claude JSON生成
- GPT JSON修復
- retry
- logger
- Driveフォルダ作成
- Driveフォルダ再利用
- Docs再利用
- Docs全削除更新
- 音声アップロード重複回避
- file_parser検証
- 不正ファイル名停止
- Google Sheets企業辞書
- Google Sheets略語辞書
- 辞書フォールバック
- GPT辞書反映
- Claude辞書反映
- 話者ラベル付与
- LINE Webhook
- LINE署名検証
- LINE通知
- LINE Q&A
- 内部状態管理
- 内部状態異常安全応答

---

# 今後の拡張候補

- 話者分離精度向上
- 過去議事録検索
- 会議横断Q&A

test