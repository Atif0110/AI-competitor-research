"""Central configuration loaded from environment variables and .env.

Provider credentials are never hard-coded.  Claude, OpenAI/GPT, and Groq can be
used independently or as a resilient fallback chain.  Model IDs and optional
base URLs are configurable so the project can survive provider model changes.
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
_PROXY_REGION_ENV = {r: f"PROXY_{r}" for r in _REGIONS}


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _str_env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _list_env(name: str) -> List[str]:
    raw = os.getenv(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _region_proxies() -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    raw_json = os.getenv("PROXY_REGIONS_JSON")
    if raw_json:
        try:
            out = {k.upper(): list(v) for k, v in json.loads(raw_json).items()}
        except (json.JSONDecodeError, TypeError):
            out = {}
    for region, env in _PROXY_REGION_ENV.items():
        vals = _list_env(env)
        if vals:
            out[region] = vals
    return out


def _exchange_rates() -> Dict[str, float]:
    defaults = {
        "USD": 1.0, "EUR": 1.08, "GBP": 1.27, "INR": 0.012, "JPY": 0.0069,
        "CAD": 0.73, "AUD": 0.66, "SGD": 0.74, "BRL": 0.18,
    }
    raw_json = os.getenv("EXCHANGE_RATES_JSON")
    if raw_json:
        try:
            defaults.update({k.upper(): float(v) for k, v in json.loads(raw_json).items()})
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return defaults


@dataclass
class Settings:
    app_name: str = field(default_factory=lambda: _str_env("APP_NAME", "AI Competitor Research Tool"))
    log_level: str = field(default_factory=lambda: _str_env("LOG_LEVEL", "INFO"))

    # Scraping
    firecrawl_api_key: str | None = field(default_factory=lambda: os.getenv("FIRECRAWL_API_KEY") or None)
    firecrawl_base_url: str = field(default_factory=lambda: _str_env("FIRECRAWL_BASE_URL", "https://api.firecrawl.dev/v1"))
    proxy_urls: List[str] = field(default_factory=lambda: _list_env("PROXY_URLS"))
    proxy_regions: Dict[str, List[str]] = field(default_factory=_region_proxies)
    proxy_verify: bool = field(default_factory=lambda: _bool_env("PROXY_VERIFY", False))

    # Resilience
    max_retries: int = field(default_factory=lambda: int(os.getenv("MAX_RETRIES", "3")))
    backoff_base_seconds: float = field(default_factory=lambda: float(os.getenv("BACKOFF_BASE_SECONDS", "1.5")))
    request_timeout_seconds: int = field(default_factory=lambda: int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30")))
    rate_limit_min_interval: float = field(default_factory=lambda: float(os.getenv("RATE_LIMIT_MIN_INTERVAL", "1.0")))
    price_drop_alert_pct: float = field(default_factory=lambda: float(os.getenv("PRICE_DROP_ALERT_PCT", "10.0")))
    min_scrape_success_rate: float = field(default_factory=lambda: float(os.getenv("MIN_SCRAPE_SUCCESS_RATE", "0.8")))

    # LLM providers.  These defaults are intentionally environment-resolved at
    # instance creation time so .env is loaded before values are read.
    llm_provider: str = field(default_factory=lambda: _str_env("LLM_PROVIDER", "auto").lower())
    groq_api_key: str | None = field(default_factory=lambda: os.getenv("GROQ_API_KEY") or None)
    groq_model: str = field(default_factory=lambda: _str_env("GROQ_MODEL", "openai/gpt-oss-120b"))
    groq_base_url: str = field(default_factory=lambda: _str_env("GROQ_BASE_URL", "https://api.groq.com/openai/v1"))
    openai_api_key: str | None = field(default_factory=lambda: os.getenv("OPENAI_API_KEY") or None)
    openai_model: str = field(default_factory=lambda: _str_env("OPENAI_MODEL", "gpt-5.6-luna"))
    openai_base_url: str | None = field(default_factory=lambda: os.getenv("OPENAI_BASE_URL") or None)
    anthropic_api_key: str | None = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY") or None)
    anthropic_model: str = field(default_factory=lambda: _str_env("ANTHROPIC_MODEL", "claude-sonnet-4-6"))
    anthropic_base_url: str | None = field(default_factory=lambda: os.getenv("ANTHROPIC_BASE_URL") or None)
    extraction_max_attempts: int = field(default_factory=lambda: int(os.getenv("EXTRACTION_MAX_ATTEMPTS", "3")))

    # FX
    exchange_rates: Dict[str, float] = field(default_factory=_exchange_rates)

    # Storage
    database_url: str = field(default_factory=lambda: _str_env("DATABASE_URL", "sqlite:///data/competitor.db"))
    vector_store_dir: str = field(default_factory=lambda: _str_env("VECTOR_STORE_DIR", "data/vectors"))
    embedding_model: str = field(default_factory=lambda: _str_env("EMBEDDING_MODEL", "text-embedding-3-small"))
    embedding_dimensions: int = field(default_factory=lambda: int(os.getenv("EMBEDDING_DIMENSIONS", "1536")))

    # API / frontend
    api_key: str | None = field(default_factory=lambda: os.getenv("API_KEY") or None)
    api_key_required: bool = field(default_factory=lambda: _bool_env("API_KEY_REQUIRED", True))
    cors_origins: List[str] = field(default_factory=lambda: _list_env("CORS_ORIGINS") or ["http://localhost:5173"])
    research_timeout_seconds: int = field(default_factory=lambda: int(os.getenv("RESEARCH_TIMEOUT_SECONDS", "1800")))
    research_rate_limit_per_minute: int = field(default_factory=lambda: int(os.getenv("RESEARCH_RATE_LIMIT_PER_MINUTE", "5")))

    # Output
    report_output_dir: str = field(default_factory=lambda: _str_env("REPORT_OUTPUT_DIR", "data/reports"))
    slack_webhook_url: str | None = field(default_factory=lambda: os.getenv("SLACK_WEBHOOK_URL") or None)
    alert_email_to: str | None = field(default_factory=lambda: os.getenv("ALERT_EMAIL_TO") or None)
    alert_email_from: str | None = field(default_factory=lambda: os.getenv("ALERT_EMAIL_FROM") or None)
    smtp_host: str | None = field(default_factory=lambda: os.getenv("SMTP_HOST") or None)
    smtp_port: int = field(default_factory=lambda: int(os.getenv("SMTP_PORT", "587")))
    smtp_user: str | None = field(default_factory=lambda: os.getenv("SMTP_USER") or None)
    smtp_password: str | None = field(default_factory=lambda: os.getenv("SMTP_PASSWORD") or None)

    # Scheduler
    scheduler_autostart: bool = field(default_factory=lambda: _bool_env("SCHEDULER_AUTOSTART", False))
    scheduler_poll_seconds: int = field(default_factory=lambda: int(os.getenv("SCHEDULER_POLL_SECONDS", "60")))

    @property
    def demo_mode(self) -> bool:
        return not (self.groq_api_key or self.openai_api_key or self.anthropic_api_key)

    def ensure_dirs(self) -> None:
        for d in (self.vector_store_dir, self.report_output_dir):
            os.makedirs(d, exist_ok=True)


def get_settings() -> Settings:
    # dotenv is loaded above and Settings uses default_factory, so every
    # environment value is resolved after .env has been loaded.
    return Settings()


settings = get_settings()
