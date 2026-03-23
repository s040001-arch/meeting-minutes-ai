from pathlib import Path
import re

paths = [
    ('RUN1', Path('logs/resume_check_run1.log')),
    ('RUN2', Path('logs/resume_check_run2.log')),
]
patterns = [
    re.compile(r'WHISPER_SPLIT_RESUME_STATE'),
    re.compile(r'next_chunk_index|resume_chunk_index'),
    re.compile(r'WHISPER_SPLIT_CHUNK_START: chunk_index=2'),
    re.compile(r'DRIVE_CRON_CHECKPOINT_SAVE_DONE|DRIVE_CRON_CHECKPOINT_DELETE_DONE'),
]
for tag, path in paths:
    print(f'--- {tag} ---')
    text = path.read_text(encoding='utf-8', errors='ignore')
    for line in text.splitlines():
        if any(p.search(line) for p in patterns):
            print(line)
