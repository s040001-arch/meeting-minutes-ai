import os
from pathlib import Path

from utils.logger import get_logger

logger = get_logger(__name__)
_SUPPORTED_AUDIO_EXTENSIONS = (".m4a", ".wav")


def load_audio_file(audio_file_path: str) -> str:
    logger.info(
        "AUDIO_LOADER_START: path=%s supported_extensions=%s",
        audio_file_path,
        ",".join(_SUPPORTED_AUDIO_EXTENSIONS),
    )
    path = Path(audio_file_path)
    if not path.exists() or not path.is_file():
        logger.warning("AUDIO_LOADER_FAILED: path=%s reason=file_not_found", audio_file_path)
        raise FileNotFoundError(f"Audio file not found: {audio_file_path}")

    extension = path.suffix.lower()
    if extension not in _SUPPORTED_AUDIO_EXTENSIONS:
        logger.warning(
            "AUDIO_LOADER_FAILED: path=%s extension=%s reason=unsupported_extension supported_extensions=%s",
            audio_file_path,
            extension,
            ",".join(_SUPPORTED_AUDIO_EXTENSIONS),
        )
        raise ValueError(
            f"Only {', '.join(_SUPPORTED_AUDIO_EXTENSIONS)} files are supported."
        )

    resolved_path = os.fspath(path.resolve())
    logger.info("AUDIO_LOADER_SUCCESS: path=%s extension=%s", resolved_path, extension)
    return resolved_path
