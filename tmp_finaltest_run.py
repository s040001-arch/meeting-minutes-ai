import os, sys, logging

# Route all logging to both stdout and the log file
log_path = "logs/finaltest_run.log"
os.makedirs("logs", exist_ok=True)
root_logger = logging.getLogger()
root_logger.setLevel(logging.DEBUG)
fh = logging.FileHandler(log_path, encoding="utf-8", mode="w")
fh.setLevel(logging.DEBUG)
fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
root_logger.addHandler(fh)

os.environ["MM_WHISPER_CHECKPOINT_PATH"] = "data/whisper_split_checkpoint_finaltest.json"
os.environ["WHISPER_SPLIT_MAX_CHUNKS_PER_RUN"] = "2"

from pipeline.cli_runner import run_pipeline_from_cli
audio = "audio/2026_0316_NRE_物流事業部・川口様_部門特化研修について.m4a"
result = run_pipeline_from_cli(audio, auto_selected_audio=False)
print(f"PIPELINE_RESULT: {result}")
