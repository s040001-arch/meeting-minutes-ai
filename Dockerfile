# Railway / コンテナ: Webhook + Drive ワーカー（scripts/railway_entry.sh）向け
# faster-whisper / ffmpeg による文字起こしを含むパイプラインを想定
FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x scripts/railway_entry.sh

ENV PYTHONUNBUFFERED=1
# Railway が注入して Start Command が CMD を上書きする場合でも、
# ENTRYPOINT を固定して railway_entry.sh が必ず先に動くようにする
ENTRYPOINT ["bash", "scripts/railway_entry.sh"]
# ENTRYPOINT に渡す余計な引数（スクリプトは引数を使わない）
CMD [""]
