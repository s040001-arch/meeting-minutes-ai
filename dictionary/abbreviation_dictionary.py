from typing import Dict, List, Tuple

from googleapiclient.discovery import build

from config.settings import settings
from utils.logger import get_logger

logger = get_logger(__name__)

LOCAL_ABBREVIATION_DICTIONARY: Dict[str, str] = {
    "crm": "CRM",
    "saas": "SaaS",
    "エスエーエーエス": "SaaS",
}


def _load_abbreviation_dictionary_from_sheets() -> List[Tuple[str, str]]:
    sheet_id = settings.GOOGLE_SHEETS_ABBREVIATION_DICTIONARY_ID
    if not sheet_id:
        return []

    service = build("sheets", "v4", credentials=settings.get_google_credentials())
    response = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=sheet_id,
            range=settings.GOOGLE_SHEETS_ABBREVIATION_DICTIONARY_RANGE,
        )
        .execute()
    )

    values = response.get("values", [])
    rows: List[Tuple[str, str]] = []
    for row in values:
        if len(row) < 2:
            continue
        source = str(row[0]).strip()
        preferred = str(row[1]).strip()
        if source and preferred:
            rows.append((source, preferred))
    return rows


def load_abbreviation_dictionary():
    try:
        rows = _load_abbreviation_dictionary_from_sheets()
        if rows:
            logger.info("Loaded abbreviation dictionary from Google Sheets: %s", len(rows))
            return rows
    except Exception as e:
        logger.warning(
            "Failed to load abbreviation dictionary from Sheets, fallback to local: %s",
            e,
        )

    logger.info("Loaded local abbreviation dictionary: %s", len(LOCAL_ABBREVIATION_DICTIONARY))
    return list(LOCAL_ABBREVIATION_DICTIONARY.items())
