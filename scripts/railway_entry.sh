#!/usr/bin/env bash
# Railway 本番用: OAuth ファイル生成 →（任意）Drive 常駐ワーカー → Uvicorn
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONUNBUFFERED=1

echo "[railway_entry] started"
python railway_bootstrap.py

DRIVE_FOLDER_ID_PRESENT=0
if [[ -n "${DRIVE_FOLDER_ID:-}" ]]; then
  DRIVE_FOLDER_ID_PRESENT=1
fi
echo "[railway_entry] env DRIVE_FOLDER_ID_present=${DRIVE_FOLDER_ID_PRESENT}"
echo "[railway_entry] env DRIVE_POLL_INTERVAL_SEC=${DRIVE_POLL_INTERVAL_SEC:-<unset>}"

if [[ -n "${DRIVE_FOLDER_ID:-}" ]]; then
  INTERVAL="${DRIVE_POLL_INTERVAL_SEC:-120}"
  echo "[railway_entry] starting drive_auto_run_forever folder_id=${DRIVE_FOLDER_ID} interval_sec=${INTERVAL}"
  python drive_auto_run_forever.py \
    --folder-id "${DRIVE_FOLDER_ID}" \
    --credentials "${GOOGLE_DRIVE_CREDENTIALS_PATH:-credentials.json}" \
    --token "${GOOGLE_DRIVE_TOKEN_PATH:-token_drive.json}" \
    --state "${DRIVE_STATE_PATH:-data/last_seen_file_ids.json}" \
    --download-dir "${DRIVE_DOWNLOAD_DIR:-data/incoming_audio}" \
    --update-state \
    --interval-sec "${INTERVAL}" &
fi

PORT="${PORT:-8000}"
echo "[railway_entry] starting uvicorn port=${PORT}"
exec uvicorn webhook_app:app --host 0.0.0.0 --port "${PORT}"
