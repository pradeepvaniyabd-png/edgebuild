"""Application settings.

Every provider credential is optional: the app must boot with all of them empty.
App settings use the ``SAX_`` prefix; provider credentials keep their conventional names.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def _alias(*names: str) -> AliasChoices:
    return AliasChoices(*names)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SAX_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        populate_by_name=True,
    )

    # --- application ---------------------------------------------------------
    env: Literal["development", "production", "test"] = "development"
    home: Path = Path("~/.super-agent-x")
    workspace_dir: Path | None = None
    allowed_roots: str = ""
    database_url: str | None = None
    auto_migrate: bool | None = None
    log_level: str = "INFO"

    # --- execution -----------------------------------------------------------
    max_concurrency: int = Field(4, ge=1, le=64)
    provider_concurrency: int = Field(2, ge=1, le=32)
    task_timeout: float = Field(900.0, gt=0)
    max_retries: int = Field(2, ge=0, le=10)
    context_token_budget: int = Field(6000, ge=500)
    llm_planning: bool = True
    creative_quality: Literal["draft", "standard", "high"] = "standard"

    # --- approval / security -------------------------------------------------
    approval_mode: Literal["interactive", "queue", "deny"] = "queue"
    auto_approve: str = ""
    shell_allow: str = ""
    redact_pii: bool = True
    allow_private_network: bool = False

    # --- budgets (USD) -------------------------------------------------------
    daily_budget_usd: float = Field(2.0, ge=0)
    project_budget_usd: float = Field(2.0, ge=0)
    per_generation_budget_usd: float = Field(0.5, ge=0)
    unpriced_estimate_usd: float = Field(0.05, ge=0)
    prices_file: Path | None = None

    # --- API -----------------------------------------------------------------
    api_host: str = "127.0.0.1"
    api_port: int = 8765
    api_token: SecretStr | None = None
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    rate_limit_per_minute: int = Field(240, ge=1)
    max_upload_mb: int = Field(50, ge=1)

    # --- office profile used by the government document templates -----------
    office_name: str = ""
    office_address: str = ""
    officer_designation: str = ""

    # --- LLM providers -------------------------------------------------------
    anthropic_api_key: SecretStr | None = Field(None, validation_alias=_alias("ANTHROPIC_API_KEY"))
    anthropic_model: str = Field("claude-opus-5-5", validation_alias=_alias("ANTHROPIC_MODEL"))
    anthropic_effort: str = Field("medium", validation_alias=_alias("ANTHROPIC_EFFORT"))
    anthropic_refusal_fallback: bool = Field(True, validation_alias=_alias("ANTHROPIC_REFUSAL_FALLBACK"))

    # Free-tier OpenAI-compatible presets. With no *_MODEL set, a model is picked from the live /models list.
    groq_api_key: SecretStr | None = Field(None, validation_alias=_alias("GROQ_API_KEY"))
    groq_model: str | None = Field(None, validation_alias=_alias("GROQ_MODEL"))
    openrouter_api_key: SecretStr | None = Field(None, validation_alias=_alias("OPENROUTER_API_KEY"))
    openrouter_model: str | None = Field(None, validation_alias=_alias("OPENROUTER_MODEL"))  # unset: free models only
    gemini_api_key: SecretStr | None = Field(None, validation_alias=_alias("GEMINI_API_KEY", "GOOGLE_API_KEY"))
    gemini_model: str | None = Field(None, validation_alias=_alias("GEMINI_MODEL"))

    openai_api_key: SecretStr | None = Field(None, validation_alias=_alias("OPENAI_API_KEY"))
    openai_base_url: str = Field("https://api.openai.com/v1", validation_alias=_alias("OPENAI_BASE_URL"))
    openai_model: str | None = Field(None, validation_alias=_alias("OPENAI_MODEL"))
    openai_image_model: str | None = Field(None, validation_alias=_alias("OPENAI_IMAGE_MODEL"))
    openai_tts_model: str | None = Field(None, validation_alias=_alias("OPENAI_TTS_MODEL"))
    openai_tts_voice: str | None = Field(None, validation_alias=_alias("OPENAI_TTS_VOICE"))
    openai_embed_model: str | None = Field(None, validation_alias=_alias("OPENAI_EMBED_MODEL"))

    # Any OpenAI-compatible endpoint: OmniRoute, FreeLLMAPI, LM Studio, LocalAI, vLLM ...
    gateway_base_url: str | None = None
    gateway_api_key: SecretStr | None = None
    gateway_model: str | None = None
    gateway_tier: Literal["local", "free", "cloud"] = "free"
    gateway_image_model: str | None = None
    gateway_tts_model: str | None = None
    gateway_tts_voice: str | None = None
    gateway_embed_model: str | None = None

    ollama_base_url: str | None = Field(None, validation_alias=_alias("OLLAMA_BASE_URL"))
    ollama_model: str | None = Field(None, validation_alias=_alias("OLLAMA_MODEL"))
    ollama_embed_model: str | None = Field(None, validation_alias=_alias("OLLAMA_EMBED_MODEL"))
    ollama_autodetect: bool = True

    aws_access_key_id: SecretStr | None = Field(None, validation_alias=_alias("AWS_ACCESS_KEY_ID"))
    aws_secret_access_key: SecretStr | None = Field(None, validation_alias=_alias("AWS_SECRET_ACCESS_KEY"))
    aws_region: str | None = Field(None, validation_alias=_alias("AWS_REGION"))
    bedrock_model_id: str | None = Field(None, validation_alias=_alias("BEDROCK_MODEL_ID"))

    # --- media providers -----------------------------------------------------
    replicate_api_token: SecretStr | None = Field(None, validation_alias=_alias("REPLICATE_API_TOKEN"))
    replicate_image_model: str = Field(
        "black-forest-labs/flux-schnell", validation_alias=_alias("REPLICATE_IMAGE_MODEL")
    )
    replicate_video_model: str | None = Field(None, validation_alias=_alias("REPLICATE_VIDEO_MODEL"))
    replicate_video_image_key: str = Field("image", validation_alias=_alias("REPLICATE_VIDEO_IMAGE_KEY"))
    replicate_image_input: str | None = Field(None, validation_alias=_alias("REPLICATE_IMAGE_INPUT"))
    replicate_video_input: str | None = Field(None, validation_alias=_alias("REPLICATE_VIDEO_INPUT"))
    replicate_api_base: str = Field("https://api.replicate.com/v1", validation_alias=_alias("REPLICATE_API_BASE"))

    fal_key: SecretStr | None = Field(None, validation_alias=_alias("FAL_KEY"))
    fal_image_model: str = Field("fal-ai/flux/schnell", validation_alias=_alias("FAL_IMAGE_MODEL"))
    fal_video_model: str | None = Field(None, validation_alias=_alias("FAL_VIDEO_MODEL"))
    fal_video_image_key: str = Field("image_url", validation_alias=_alias("FAL_VIDEO_IMAGE_KEY"))
    fal_image_input: str | None = Field(None, validation_alias=_alias("FAL_IMAGE_INPUT"))
    fal_video_input: str | None = Field(None, validation_alias=_alias("FAL_VIDEO_INPUT"))
    fal_queue_base: str = Field("https://queue.fal.run", validation_alias=_alias("FAL_QUEUE_BASE"))

    comfyui_base_url: str | None = Field(None, validation_alias=_alias("COMFYUI_BASE_URL"))
    comfyui_image_workflow: Path | None = Field(None, validation_alias=_alias("COMFYUI_IMAGE_WORKFLOW"))
    comfyui_video_workflow: Path | None = Field(None, validation_alias=_alias("COMFYUI_VIDEO_WORKFLOW"))

    piper_bin: str = Field("piper", validation_alias=_alias("PIPER_BIN"))
    piper_model: Path | None = Field(None, validation_alias=_alias("PIPER_MODEL"))
    espeak_bin: str = Field("espeak-ng", validation_alias=_alias("ESPEAK_BIN"))
    edge_tts_enabled: bool = False

    # --- web -----------------------------------------------------------------
    web_search: Literal["duckduckgo", "searxng", "none"] = "duckduckgo"
    searxng_url: str | None = Field(None, validation_alias=_alias("SEARXNG_URL"))

    # ------------------------------------------------------------------ paths
    @property
    def home_path(self) -> Path:
        return self.home.expanduser().resolve()

    @property
    def workspace(self) -> Path:
        base = self.workspace_dir or (self.home_path / "workspace")
        return Path(base).expanduser().resolve()

    @property
    def projects_dir(self) -> Path:
        return self.workspace / "projects"

    @property
    def uploads_dir(self) -> Path:
        return self.workspace / "uploads"

    @property
    def outputs_dir(self) -> Path:
        return self.workspace / "outputs"

    @property
    def cache_dir(self) -> Path:
        return self.home_path / "cache"

    @property
    def logs_dir(self) -> Path:
        return self.home_path / "logs"

    @property
    def read_roots(self) -> list[Path]:
        return [Path(p).expanduser().resolve() for p in _csv(self.allowed_roots)]

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite+aiosqlite:///{self.home_path / 'superagent.db'}"

    @property
    def is_sqlite(self) -> bool:
        return self.db_url.startswith("sqlite")

    @property
    def should_auto_migrate(self) -> bool:
        if self.auto_migrate is not None:
            return self.auto_migrate
        return self.is_sqlite

    @property
    def auto_approve_set(self) -> set[str]:
        return set(_csv(self.auto_approve))

    @property
    def cors_origin_list(self) -> list[str]:
        return _csv(self.cors_origins)

    def ensure_dirs(self) -> None:
        for path in (
            self.home_path,
            self.workspace,
            self.projects_dir,
            self.uploads_dir,
            self.outputs_dir,
            self.cache_dir,
            self.logs_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            try:
                self.home_path.chmod(0o700)  # memories, approvals and outputs are private to the owner
            except OSError:
                pass

    def secret_values(self) -> list[str]:
        """Configured secret strings, used to mask them wherever they might surface."""
        values: list[str] = []
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                raw = value.get_secret_value()
                if len(raw) >= 6:
                    values.append(raw)
        return values


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
