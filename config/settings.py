import os
from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv
from google.auth.credentials import Credentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials


BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"

load_dotenv(ENV_PATH)


class Settings:
    def __init__(self) -> None:
        self.OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
        self.WHISPER_MODEL: str = os.getenv("WHISPER_MODEL", "gpt-4o-transcribe")
        self.WHISPER_LANGUAGE: str = os.getenv("WHISPER_LANGUAGE", "ja")
        self.WHISPER_CHUNK_MINUTES: int = int(os.getenv("WHISPER_CHUNK_MINUTES", "20"))
        self.WHISPER_UTTERANCE_PAUSE_SEC: float = float(
            os.getenv("WHISPER_UTTERANCE_PAUSE_SEC", "1.2")
        )
        self.WHISPER_UTTERANCE_MAX_CHARS: int = int(
            os.getenv("WHISPER_UTTERANCE_MAX_CHARS", "160")
        )

        self.OPENAI_GPT_PREPROCESS_MODEL: str = os.getenv(
            "OPENAI_GPT_PREPROCESS_MODEL",
            "gpt-4.1-mini",
        )
        self.GPT_PREPROCESS_MODEL: str = os.getenv(
            "GPT_PREPROCESS_MODEL",
            self.OPENAI_GPT_PREPROCESS_MODEL,
        )
        self.GPT_PREPROCESS_TEMPERATURE: float = float(
            os.getenv("GPT_PREPROCESS_TEMPERATURE", "0")
        )

        self.ENABLE_PROVISIONAL_SPEAKER_LABELING: bool = self._get_bool(
            "ENABLE_PROVISIONAL_SPEAKER_LABELING",
            True,
        )
        self.SPEAKER_LABELING_HEURISTIC_THRESHOLD: float = float(
            os.getenv("SPEAKER_LABELING_HEURISTIC_THRESHOLD", "0.60")
        )
        self.SPEAKER_LABELING_FORCE_ASSIGN: bool = self._get_bool(
            "SPEAKER_LABELING_FORCE_ASSIGN",
            True,
        )
        self.GPT_SPEAKER_RULE_HINT_STRENGTH: str = os.getenv(
            "GPT_SPEAKER_RULE_HINT_STRENGTH",
            "standard",
        ).strip().lower()
        self.GPT_SPEAKER_RULE_CUSTOMER_THRESHOLD: float = float(
            os.getenv("GPT_SPEAKER_RULE_CUSTOMER_THRESHOLD", "0.55")
        )
        self.GPT_SPEAKER_RULE_PRECENA_THRESHOLD: float = float(
            os.getenv("GPT_SPEAKER_RULE_PRECENA_THRESHOLD", "0.55")
        )
        self.GPT_SPEAKER_RULE_AMBIGUOUS_THRESHOLD: float = float(
            os.getenv("GPT_SPEAKER_RULE_AMBIGUOUS_THRESHOLD", "0.10")
        )

        self.ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
        self.CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-3-haiku-20240307")
        self.CLAUDE_MAX_TOKENS: int = int(os.getenv("CLAUDE_MAX_TOKENS", "4000"))
        self.CLAUDE_TEMPERATURE: float = float(os.getenv("CLAUDE_TEMPERATURE", "0"))

        self.GOOGLE_DRIVE_BASE_FOLDER_ID: str = os.getenv("GOOGLE_DRIVE_BASE_FOLDER_ID", "")
        if not self.GOOGLE_DRIVE_BASE_FOLDER_ID:
            self.GOOGLE_DRIVE_BASE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_ROOT_FOLDER_ID", "")
        self.ENABLE_GOOGLE_DOCS_WRITE: bool = self._get_bool(
            "ENABLE_GOOGLE_DOCS_WRITE",
            True,
        )
        self.GOOGLE_SHEETS_COMPANY_DICTIONARY_ID: str = os.getenv(
            "GOOGLE_SHEETS_COMPANY_DICTIONARY_ID", ""
        )
        self.GOOGLE_SHEETS_COMPANY_DICTIONARY_RANGE: str = os.getenv(
            "GOOGLE_SHEETS_COMPANY_DICTIONARY_RANGE",
            "company_dictionary!A:B",
        )
        self.GOOGLE_SHEETS_ABBREVIATION_DICTIONARY_ID: str = os.getenv(
            "GOOGLE_SHEETS_ABBREVIATION_DICTIONARY_ID", ""
        )
        self.GOOGLE_SHEETS_ABBREVIATION_DICTIONARY_RANGE: str = os.getenv(
            "GOOGLE_SHEETS_ABBREVIATION_DICTIONARY_RANGE",
            "abbreviation_dictionary!A:B",
        )

        self.LINE_CHANNEL_ACCESS_TOKEN: str = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
        self.LINE_CHANNEL_SECRET: str = os.getenv("LINE_CHANNEL_SECRET", "")
        self.LINE_NOTIFY_ENABLED: bool = self._get_bool("LINE_NOTIFY_ENABLED", True)

        self.LATEST_MINUTES_STATE_FILE: str = os.getenv(
            "LATEST_MINUTES_STATE_FILE",
            str(BASE_DIR / "data" / "latest_minutes_state.json"),
        )

        self.LOG_DIR: str = os.getenv("LOG_DIR", str(BASE_DIR / "logs"))
        self.LOG_FILE: str = os.getenv("LOG_FILE", str(Path(self.LOG_DIR) / "meeting.log"))
        self.PROCESSED_AUDIO_LOG: str = os.getenv(
            "PROCESSED_AUDIO_LOG",
            str(BASE_DIR / "processed_audio.log"),
        )

    def _get_bool(self, key: str, default: bool = False) -> bool:
        value = os.getenv(key)
        if value is None:
            return default
        return value.strip().lower() in {"1", "true", "yes", "on"}

    def get_google_credentials(self) -> Credentials:
        import json

        scopes = [
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/documents",
        ]

        service_account_json_str = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        if not service_account_json_str:
            raise ValueError(
                "GOOGLE_SERVICE_ACCOUNT_JSON environment variable is not set. "
                "Please provide Service Account JSON as a string."
            )

        try:
            service_account_info = json.loads(service_account_json_str)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Failed to parse GOOGLE_SERVICE_ACCOUNT_JSON as JSON: {exc}"
            )

        credentials = ServiceAccountCredentials.from_service_account_info(
            service_account_info,
            scopes=scopes,
        )
        return credentials

    def get_speaker_labeling_config(self) -> Dict[str, Any]:
        return {
            "enabled": self.ENABLE_PROVISIONAL_SPEAKER_LABELING,
            "heuristic_threshold": self.SPEAKER_LABELING_HEURISTIC_THRESHOLD,
            "force_assign": self.SPEAKER_LABELING_FORCE_ASSIGN,
            "gpt_rule_hint_strength": self.GPT_SPEAKER_RULE_HINT_STRENGTH,
            "customer_threshold": self.GPT_SPEAKER_RULE_CUSTOMER_THRESHOLD,
            "precena_threshold": self.GPT_SPEAKER_RULE_PRECENA_THRESHOLD,
            "ambiguous_threshold": self.GPT_SPEAKER_RULE_AMBIGUOUS_THRESHOLD,
        }

    def get_gpt_speaker_rule_prompt_block(self) -> str:
        strength = self.GPT_SPEAKER_RULE_HINT_STRENGTH

        if strength == "strict":
            mode_text = "厳格"
            guidance = [
                "話者推定ルールを強く適用してください。",
                "顧客側シグナルまたはプレセナ側シグナルが少しでも明確なら、その側へ寄せて判定してください。",
                "曖昧でも最終的には必ずどちらかを付与してください。",
            ]
        elif strength == "loose":
            mode_text = "緩め"
            guidance = [
                "話者推定ルールは参考情報として使ってください。",
                "文脈全体を優先しつつ、明確なシグナルがある場合のみ強く寄せてください。",
                "曖昧な場合でも最終的にはどちらかを付与してください。",
            ]
        else:
            mode_text = "標準"
            guidance = [
                "話者推定ルールを標準強度で適用してください。",
                "顧客側シグナルとプレセナ側シグナルのバランスを見て判断してください。",
                "曖昧な場合でも最終的にはどちらかを付与してください。",
            ]

        return "\n".join(
            [
                "【仮話者ラベル推定ルール設定】",
                f"- ラベル付与機能: {'有効' if self.ENABLE_PROVISIONAL_SPEAKER_LABELING else '無効'}",
                f"- 推定ルール強度: {mode_text}",
                f"- 顧客判定閾値: {self.GPT_SPEAKER_RULE_CUSTOMER_THRESHOLD:.2f}",
                f"- プレセナ判定閾値: {self.GPT_SPEAKER_RULE_PRECENA_THRESHOLD:.2f}",
                f"- 曖昧判定幅: {self.GPT_SPEAKER_RULE_AMBIGUOUS_THRESHOLD:.2f}",
                f"- ヒューリスティック閾値: {self.SPEAKER_LABELING_HEURISTIC_THRESHOLD:.2f}",
                f"- 強制割当: {'有効' if self.SPEAKER_LABELING_FORCE_ASSIGN else '無効'}",
                *[f"- {line}" for line in guidance],
            ]
        )


settings = Settings()
OPENAI_API_KEY = settings.OPENAI_API_KEY