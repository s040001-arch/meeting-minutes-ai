from pathlib import Path
from typing import Dict, Optional

from utils.logger import get_logger

logger = get_logger(__name__)
_SUPPORTED_METADATA_EXTENSIONS = (".m4a", ".wav")


def parse_audio_filename(audio_file_path: str) -> Optional[Dict[str, str]]:
    name = Path(audio_file_path).name
    extension = Path(name).suffix.lower()
    if extension not in _SUPPORTED_METADATA_EXTENSIONS:
        logger.warning(
            "AUDIO_FILENAME_PARSE_FAILED: path=%s extension=%s reason=unsupported_extension supported_extensions=%s",
            audio_file_path,
            extension,
            ",".join(_SUPPORTED_METADATA_EXTENSIONS),
        )
        return None

    stem = Path(name).stem
    parts = stem.split("_", 3)
    if len(parts) != 4:
        logger.warning(
            "AUDIO_FILENAME_PARSE_FAILED: path=%s extension=%s reason=invalid_parts_count parts_count=%s",
            audio_file_path,
            extension,
            len(parts),
        )
        return None

    year, mmdd, customer_name, meeting_title = parts
    if len(year) != 4 or len(mmdd) != 4 or not year.isdigit() or not mmdd.isdigit():
        logger.warning(
            "AUDIO_FILENAME_PARSE_FAILED: path=%s extension=%s reason=invalid_date_format year=%s mmdd=%s",
            audio_file_path,
            extension,
            year,
            mmdd,
        )
        return None

    meeting_info = {
        "date": f"{year}_{mmdd}",
        "customer_name": customer_name.strip(),
        "meeting_title": meeting_title.strip(),
    }
    logger.info(
        "AUDIO_FILENAME_PARSE_SUCCESS: path=%s extension=%s meeting_info=%s",
        audio_file_path,
        extension,
        meeting_info,
    )
    return meeting_info
