from pathlib import Path
from typing import Dict, Optional


def parse_audio_filename(audio_file_path: str) -> Optional[Dict[str, str]]:
    name = Path(audio_file_path).name
    if not name.lower().endswith(".m4a"):
        return None

    stem = Path(name).stem
    parts = stem.split("_", 3)
    if len(parts) != 4:
        return None

    year, mmdd, customer_name, meeting_title = parts
    if len(year) != 4 or len(mmdd) != 4 or not year.isdigit() or not mmdd.isdigit():
        return None

    return {
        "date": f"{year}_{mmdd}",
        "customer_name": customer_name.strip(),
        "meeting_title": meeting_title.strip(),
    }
