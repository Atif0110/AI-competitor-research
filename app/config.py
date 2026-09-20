"""Central configuration loaded from environment variables and .env.

Provider credentials are never hard-coded. Claude, OpenAI/GPT, and Groq can be
used independently or as a resilient fallback chain.

Demo mode is explicit and must be enabled with DEMO_MODE=true. A missing
provider credential must never silently switch a production deployment into
demo mode.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


_REGIONS = ["US", "EU", "UK", "IN", "JP", "CA", "AU", "SG", "BR"]
_PROXY_REGION_ENV = {region: f"PROXY_{region}" for region in _REGIONS}


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _str_env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _optional_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None

    value = value.strip()
    return value or None


def _list_env(name: str) -> List[str]:
    raw = os.getenv(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _region_proxies() -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}

    raw_json = os.getenv("PROXY_REGIONS_JSON")

    if raw_json:
        try:
            parsed = json.loads(raw_json)

            if isinstance(parsed, dict):
                out = {
                    str(key).upper(): list(value)
                    for key, value in parsed.items()
                    if isinstance(value, list)
                }
        except (json.JSONDecodeError, TypeError, ValueError):
            out = {}

    for region, env in _PROXY_REGION_ENV.items():
        values = _list_env(env)

        if values:
            out[region] = values

    return out


def _exchange_rates() -> Dict[str, float]:
    defaults = {
        "USD": 1.0,
        "EUR": 1.08,
        "GBP": 1.27,
        "INR": 0.012,
        "JPY": 0.0069,
        "CAD": 0.73,
        "AUD": 0.66,
        "SGD": 0.74,
        "BRL": 0.18,
    }

    raw_json = os.getenv("EXCHANGE_RATES_JSON")

    if raw_json:
        try:
            parsed = json.loads(raw_json)

            if isinstance(parsed, dict):
                defaults.update(
                    {
                        str(key).upper(): float(value)
                        for key, value in parsed.items()
                    }
                )
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    return defaults


@dataclass
class Settings:
    app_name: str = field(
        default_factory=lambda: _str_env(
            "APP_NAME",
            "AI Competitor Research Tool",
        )
    )

    log_level: str = field(
        default_factory=lambda: _str_env(
            "LOG_LEVEL",
            "INFO",
        )
    )

    # ------------------------------------------------------------------
    # Scraping
    # ------------------------------------------------------------------

    firecrawl_api_key: str | None = field(
        default_factory=lambda: _optional_env("FIRECRAWL_API_KEY")
    )

    firecrawl_base_url: str = field(
        default_factory=lambda: _str_env(
            "FIRECRAWL_BASE_URL",
            "https://api.firecrawl.dev/v1",
        )
    )

    proxy_urls: List[str] = field(
        default_factory=lambda: _list_env("PROXY_URLS")
    )

    proxy_regions: Dict[str, List[str]] = field(
        default_factory=_region_proxies
    )

    proxy_verify: bool = field(
        default_factory=lambda: _bool_env(
            "PROXY_VERIFY",
            False,
        )
    )

    # ------------------------------------------------------------------
    # Resilience
    # ------------------------------------------------------------------

    max_retries: int = field(
        default_factory=lambda: int(
            os.getenv("MAX_RETRIES", "3")
        )
    )

    backoff_base_seconds: float = field(
        default_factory=lambda: float(
            os.getenv("BACKOFF_BASE_SECONDS", "1.5")
        )
    )

    request_timeout_seconds: int = field(
        default_factory=lambda: int(
            os.getenv("REQUEST_TIMEOUT_SECONDS", "30")
        )
    )

    rate_limit_min_interval: float = field(
        default_factory=lambda: float(
            os.getenv("RATE_LIMIT_MIN_INTERVAL", "1.0")
        )
    )

    price_drop_alert_pct: float = field(
        default_factory=lambda: float(
            os.getenv("PRICE_DROP_ALERT_PCT", "10.0")
        )
    )

    min_scrape_success_rate: float = field(
        default_factory=lambda: float(
            os.getenv("MIN_SCRAPE_SUCCESS_RATE", "0.8")
        )
    )

    # ------------------------------------------------------------------
    # LLM providers
    # ------------------------------------------------------------------

    # Explicit values:
    #   auto
    #   gemini
    #   groq
    #   openai
    #   anthropic
    #   demo
    llm_provider: str = field(
        default_factory=lambda: _str_env(
            "LLM_PROVIDER",
            "auto",
        ).lower()
    )

    gemini_api_key: str | None = field(
        default_factory=lambda: _optional_env("GEMINI_API_KEY")
    )

    gemini_model: str = field(
        default_factory=lambda: _str_env(
            "GEMINI_MODEL",
            "gemini-2.5-flash",
        )
    )

    gemini_base_url: str | None = field(
        default_factory=lambda: _optional_env("GEMINI_BASE_URL")
    )

    groq_api_key: str | None = field(
        default_factory=lambda: _optional_env("GROQ_API_KEY")
    )

    groq_model: str = field(
        default_factory=lambda: _str_env(
            "GROQ_MODEL",
            "openai/gpt-oss-120b",
        )
    )

    groq_base_url: str = field(
        default_factory=lambda: _str_env(
            "GROQ_BASE_URL",
            "https://api.groq.com/openai/v1",
        )
    )

    openai_api_key: str | None = field(
        default_factory=lambda: _optional_env("OPENAI_API_KEY")
    )

    openai_model: str = field(
        default_factory=lambda: _str_env(
            "OPENAI_MODEL",
            "gpt-5.6-luna",
        )
    )

    openai_base_url: str | None = field(
        default_factory=lambda: _optional_env("OPENAI_BASE_URL")
    )

    anthropic_api_key: str | None = field(
        default_factory=lambda: _optional_env("ANTHROPIC_API_KEY")
    )

    anthropic_model: str = field(
        default_factory=lambda: _str_env(
            "ANTHROPIC_MODEL",
            "claude-sonnet-4-6",
        )
    )

    anthropic_base_url: str | None = field(
        default_factory=lambda: _optional_env("ANTHROPIC_BASE_URL")
    )

    extraction_max_attempts: int = field(
        default_factory=lambda: int(
            os.getenv("EXTRACTION_MAX_ATTEMPTS", "3")
        )
    )

    # IMPORTANT:
    # Demo mode is now explicit.
    #
    # Production:
    #     DEMO_MODE=false
    #
    # Tests/offline development:
    #     DEMO_MODE=true
    demo_mode_enabled: bool = field(
        default_factory=lambda: _bool_env(
            "DEMO_MODE",
            False,
        )
    )

    # ------------------------------------------------------------------
    # FX
    # ------------------------------------------------------------------

    exchange_rates: Dict[str, float] = field(
        default_factory=_exchange_rates
    )

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    database_url: str = field(
        default_factory=lambda: _str_env(
            "DATABASE_URL",
            "sqlite:///data/competitor.db",
        )
    )

    vector_store_dir: str = field(
        default_factory=lambda: _str_env(
            "VECTOR_STORE_DIR",
            "data/vectors",
        )
    )

    embedding_model: str = field(
        default_factory=lambda: _str_env(
            "EMBEDDING_MODEL",
            "text-embedding-3-small",
        )
    )

    embedding_dimensions: int = field(
        default_factory=lambda: int(
            os.getenv("EMBEDDING_DIMENSIONS", "1536")
        )
    )

    # ------------------------------------------------------------------
    # API / frontend
    # ------------------------------------------------------------------

    api_key: str | None = field(
        default_factory=lambda: _optional_env("API_KEY")
    )

    api_key_required: bool = field(
        default_factory=lambda: _bool_env(
            "API_KEY_REQUIRED",
            True,
        )
    )

    cors_origins: List[str] = field(
        default_factory=lambda: _list_env("CORS_ORIGINS")
        or [
            "http://localhost:5173",
            "http://localhost:8080",
            "https://ai-competitor-research.onrender.com",
        ]
    )

    research_timeout_seconds: int = field(
        default_factory=lambda: int(
            os.getenv("RESEARCH_TIMEOUT_SECONDS", "1800")
        )
    )

    research_rate_limit_per_minute: int = field(
        default_factory=lambda: int(
            os.getenv("RESEARCH_RATE_LIMIT_PER_MINUTE", "5")
        )
    )

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    report_output_dir: str = field(
        default_factory=lambda: _str_env(
            "REPORT_OUTPUT_DIR",
            "data/reports",
        )
    )

    slack_webhook_url: str | None = field(
        default_factory=lambda: _optional_env("SLACK_WEBHOOK_URL")
    )

    alert_email_to: str | None = field(
        default_factory=lambda: _optional_env("ALERT_EMAIL_TO")
    )

    alert_email_from: str | None = field(
        default_factory=lambda: _optional_env("ALERT_EMAIL_FROM")
    )

    smtp_host: str | None = field(
        default_factory=lambda: _optional_env("SMTP_HOST")
    )

    smtp_port: int = field(
        default_factory=lambda: int(
            os.getenv("SMTP_PORT", "587")
        )
    )

    smtp_user: str | None = field(
        default_factory=lambda: _optional_env("SMTP_USER")
    )

    smtp_password: str | None = field(
        default_factory=lambda: _optional_env("SMTP_PASSWORD")
    )

    # ------------------------------------------------------------------
    # Scheduler
    # ------------------------------------------------------------------

    scheduler_autostart: bool = field(
        default_factory=lambda: _bool_env(
            "SCHEDULER_AUTOSTART",
            False,
        )
    )

    scheduler_poll_seconds: int = field(
        default_factory=lambda: int(
            os.getenv("SCHEDULER_POLL_SECONDS", "60")
        )
    )

    @property
    def demo_mode(self) -> bool:
        """Return whether explicit demo mode has been enabled.

        Missing LLM credentials must never silently activate demo mode.
        """

        return self.demo_mode_enabled

    @property
    def has_llm_credentials(self) -> bool:
        """Return whether at least one supported LLM credential is configured."""

        return bool(
            self.gemini_api_key
            or self.groq_api_key
            or self.openai_api_key
            or self.anthropic_api_key
        )

    def ensure_dirs(self) -> None:
        for directory in (
            self.vector_store_dir,
            self.report_output_dir,
        ):
            os.makedirs(directory, exist_ok=True)


def get_settings() -> Settings:
    # dotenv is loaded above and Settings uses default_factory, so every
    # environment value is resolved after .env has been loaded.
    return Settings()


settings = get_settings()
