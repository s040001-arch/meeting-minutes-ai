# Railway / コンテナ: Webhook + Drive ワーカー（scripts/railway_entry.sh）向け
FROM python:3.12-slim-bookworm

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY data/correction_dict.json /app/seed/correction_dict.json

RUN chmod +x scripts/railway_entry.sh

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["bash", "scripts/railway_entry.sh"]
CMD [""]
