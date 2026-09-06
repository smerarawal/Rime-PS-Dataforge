"""Centralized configuration. Secrets come only from the environment."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_provider: Literal["mock", "gemini"] = "mock"
    tts_provider: Literal["mock", "rime"] = "mock"
    realtime_provider: Literal["mock", "livekit"] = "mock"

    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

    rime_api_key: str = ""
    rime_model_id: str = "mistv2"
    rime_speaker: str = "cove"
    rime_sampling_rate: int = 24000

    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    tool_delay_seconds: float = Field(default=4.0, ge=0.0)

    def gemini_configured(self) -> bool:
        return bool(self.gemini_api_key.strip())

    def rime_configured(self) -> bool:
        return bool(self.rime_api_key.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
