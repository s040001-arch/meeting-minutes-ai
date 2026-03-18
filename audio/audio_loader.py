import os
from pathlib import Path


def load_audio_file(audio_file_path: str) -> str:
    path = Path(audio_file_path)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Audio file not found: {audio_file_path}")
    if path.suffix.lower() != ".m4a":
        raise ValueError("Only .m4a files are supported.")
    return os.fspath(path.resolve())
