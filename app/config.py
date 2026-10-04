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

    # ------------------------------------------------------------------
    # APInex (free-tier first aggregator)
    #
    # APInex exposes an OpenAI-compatible endpoint plus agent tools
    # (web search / contents / research). Free models are addressed with a
    # `free/` prefix and are covered by a daily free-token allowance that
    # resets at 00:00 UTC, so the default model is a free one.
    # ------------------------------------------------------------------

    apinex_api_key: str | None = field(
        default_factory=lambda: _optional_env("APINEX_API_KEY")
    )

    apinex_base_url: str = field(
        default_factory=lambda: _str_env(
            "APINEX_BASE_URL",
            "https://apinex.bond/v1",
        )
    )

    apinex_model: str = field(
        default_factory=lambda: _str_env(
            "APINEX_MODEL",
            "free/all",
        )
    )

    apinex_web_tools: bool = field(
        default_factory=lambda: _bool_env(
            "APINEX_WEB_TOOLS",
            True,
        )
    )

    apinex_timeout_seconds: int = field(
        default_factory=lambda: int(
            os.getenv("APINEX_TIMEOUT_SECONDS", "90")
        )
    )

    apinex_max_output_tokens: int = field(
        default_factory=lambda: int(
            os.getenv("APINEX_MAX_OUTPUT_TOKENS", "4096")
        )
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

    checkpoint_dir: str = field(
        default_factory=lambda: _str_env(
            "CHECKPOINT_DIR",
            "data/checkpoints",
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
    # Deep research
    # ------------------------------------------------------------------

    deep_max_pages: int = field(
        default_factory=lambda: int(
            os.getenv("DEEP_MAX_PAGES", "12")
        )
    )

    deep_max_pages_per_host: int = field(
        default_factory=lambda: int(
            os.getenv("DEEP_MAX_PAGES_PER_HOST", "8")
        )
    )

    deep_max_depth: int = field(
        default_factory=lambda: int(
            os.getenv("DEEP_MAX_DEPTH", "2")
        )
    )

    deep_concurrency: int = field(
        default_factory=lambda: int(
            os.getenv("DEEP_CONCURRENCY", "3")
        )
    )

    deep_per_host_delay: float = field(
        default_factory=lambda: float(
            os.getenv("DEEP_PER_HOST_DELAY", "1.0")
        )
    )

    deep_page_char_budget: int = field(
        default_factory=lambda: int(
            os.getenv("DEEP_PAGE_CHAR_BUDGET", "14000")
        )
    )

    deep_store_char_budget: int = field(
        default_factory=lambda: int(
            os.getenv("DEEP_STORE_CHAR_BUDGET", "40000")
        )
    )

    deep_respect_robots: bool = field(
        default_factory=lambda: _bool_env(
            "DEEP_RESPECT_ROBOTS",
            True,
        )
    )

    deep_min_content_chars: int = field(
        default_factory=lambda: int(
            os.getenv("DEEP_MIN_CONTENT_CHARS", "400")
        )
    )

    deep_section_min_chars: int = field(
        default_factory=lambda: int(
            os.getenv("DEEP_SECTION_MIN_CHARS", "120")
        )
    )

    deep_chunk_chars: int = field(
        default_factory=lambda: int(
            os.getenv("DEEP_CHUNK_CHARS", "1100")
        )
    )

    deep_chunk_overlap: int = field(
        default_factory=lambda: int(
            os.getenv("DEEP_CHUNK_OVERLAP", "150")
        )
    )

    deep_llm_enabled: bool = field(
        default_factory=lambda: _bool_env(
            "DEEP_LLM_ENABLED",
            True,
        )
    )

    deep_search_enabled: bool = field(
        default_factory=lambda: _bool_env(
            "DEEP_SEARCH_ENABLED",
            True,
        )
    )

    # ------------------------------------------------------------------
    # Research chat
    # ------------------------------------------------------------------

    chat_top_k: int = field(
        default_factory=lambda: int(
            os.getenv("CHAT_TOP_K", "6")
        )
    )

    chat_context_chars: int = field(
        default_factory=lambda: int(
            os.getenv("CHAT_CONTEXT_CHARS", "14000")
        )
    )

    chat_history_turns: int = field(
        default_factory=lambda: int(
            os.getenv("CHAT_HISTORY_TURNS", "6")
        )
    )

    chat_min_relevance: float = field(
        default_factory=lambda: float(
            os.getenv("CHAT_MIN_RELEVANCE", "0.15")
        )
    )

    # Share of a question's vocabulary that must appear in the retrieved
    # evidence before an extractive (no-model) answer is allowed to answer.
    chat_min_coverage: float = field(
        default_factory=lambda: float(
            os.getenv("CHAT_MIN_COVERAGE", "0.34")
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
            or self.apinex_api_key
        )

    # Free allowance tiers first, metered/paid providers last. The provider
    # router walks this order so a deployment with no budget keeps working
    # on free models and only touches paid keys when free ones fail.
    FREE_TIER_PROVIDERS = ("apinex", "groq", "gemini")
    PAID_PROVIDERS = ("anthropic", "openai")

    @property
    def provider_tiers(self) -> Dict[str, str]:
        """provider -> 'free' | 'paid' for every provider with a configured key."""

        configured = {
            "apinex": bool(self.apinex_api_key),
            "groq": bool(self.groq_api_key),
            "gemini": bool(self.gemini_api_key),
            "anthropic": bool(self.anthropic_api_key),
            "openai": bool(self.openai_api_key),
        }
        return {
            name: ("free" if name in self.FREE_TIER_PROVIDERS else "paid")
            for name, configured_flag in configured.items()
            if configured_flag
        }

    def ensure_dirs(self) -> None:
        for directory in (
            self.vector_store_dir,
            self.report_output_dir,
            self.checkpoint_dir,
        ):
            os.makedirs(directory, exist_ok=True)


def get_settings() -> Settings:
    # dotenv is loaded above and Settings uses default_factory, so every
    # environment value is resolved after .env has been loaded.
    return Settings()


settings = get_settings()
