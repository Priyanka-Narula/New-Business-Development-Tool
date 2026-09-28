"""Environment-backed application settings."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_URL = (
    f"sqlite:///{(PROJECT_ROOT / 'aambridge.db').as_posix()}"
)


class Settings(BaseSettings):
    """Settings loaded from environment variables and the local ``.env`` file."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    linkedin_email: str = ""
    linkedin_password: str = ""
    database_url: str = DEFAULT_DATABASE_URL
    database_echo: bool = False
    log_dir: Path = PROJECT_ROOT / "logs"
    log_level: str = "INFO"
    log_max_bytes: int = Field(default=5_000_000, ge=1)
    log_backup_count: int = Field(default=5, ge=0)
    ollama_url: str = "http://localhost:11434/api/chat"
    extraction_model: str = "llama3.1"
    extraction_max_chars: int = Field(default=12_000, ge=1_000)
    extraction_max_attempts: int = Field(default=3, ge=1)

    @property
    def has_linkedin_credentials(self) -> bool:
        """Return whether both LinkedIn credential values are available."""

        return bool(self.linkedin_email.strip() and self.linkedin_password)

