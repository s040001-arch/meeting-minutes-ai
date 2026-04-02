# Meeting Minutes AI

Google Drive に音声ファイルを置くだけで、文字起こし → AI 補正 → LINE 質問 → 議事録生成 → Google Docs 出力まで自動で行うシステム。

## アーキテクチャ

```
Google Drive (audio)
  ↓ polling (120s)
drive_auto_run_forever.py
  ↓
run_job_once.py  ── 16-step pipeline
  │
  ├─ 2.1  ffmpeg_convert_to_wav.py
  ├─ 3.0  audio_split_chunks.py
  ├─ 3.1  transcribe_one_chunk.py  (Whisper)
  ├─ 4.1  transcription_merge_chunks.py
  ├─ 4.0  Google Docs 作成 (【文字起こし】)
  ├─ 4.2  mechanical_correct_text.py
  ├─ 4.3  ai_correct_text.py  (Claude 3.5 Sonnet)
  ├─ 4.4  extract_unknown_points.py
  ├─ 5.0  run_question_cycle_once.py  (GPT → LINE)
  ├─ 5.4  recorrect_from_line_answer.py
  ├─ 6.1  generate_minutes_transcript.py
  ├─ 6.2  generate_minutes_other_sections.py  (GPT-4o)
  └─ 6.3  export_minutes_to_google_docs.py
```

## 必要な環境変数

| 変数名 | 用途 |
|--------|------|
| `ANTHROPIC_API_KEY` | AI補正 (Claude 3.5 Sonnet) |
| `OPENAI_API_KEY` | 質問生成・議事録生成 |
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE push通知 |
| `LINE_USER_ID` | 質問の送信先 |
| `DRIVE_FOLDER_ID` | 監視対象のGoogle Driveフォルダ |
| `KNOWLEDGE_SHEET_ID` | 回答由来ナレッジを蓄積する Google Sheets |

詳細は `.env.example` を参照。

## ローカル実行

```bash
pip install -r requirements.txt

# 単発実行（音声ファイル指定）
python run_job_once.py --source path/to/audio.m4a

# Drive監視の常駐実行
python drive_auto_run_forever.py
```

## Railway デプロイ

1. リポジトリを Railway に接続（Dockerfile ビルド）
2. Variables に環境変数を設定（上記 + Google OAuth JSON 3つ）
3. `railway_bootstrap.py` が起動時に認証ファイルを書き出し
4. `drive_auto_run_forever.py` が自動でポーリング開始
5. Volume を `/app/data` にマウント推奨（中間ファイル永続化）

## 主な技術スタック

- Python 3.12 / FastAPI (health check)
- Whisper (文字起こし)
- Claude 3.5 Sonnet (AI補正)
- GPT-4o (質問生成・議事録生成)
- LINE Messaging API (質問・回答)
- Google Drive API / Docs API

