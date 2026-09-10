"""Central configuration — every knob is overridable via environment variables.

Copy `.env.example` to `.env` and fill in keys, or export the variables.
The pipeline runs in **demo mode** automatically when no LLM keys are set.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List

_REGIONS = ["US", "EU", "UK", "IN", "JP", "CA", "AU", "SG", "BR"]
_PROXY_REGION_ENV = {r: f"PROXY_{r}" for r in _REGIONS}


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _list_env(name: str) -> List[str]:
    raw = os.getenv(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _region_proxies() -> Dict[str, List[str]]:
    """Structured geo-proxy config: PROXY_US=..., PROXY_IN=..., etc.
    Also accepts PROXY_REGIONS_JSON='{"IN": [...], "UK": [...]}'."""
    out: Dict[str, List[str]] = {}
    raw_json = os.getenv("PROXY_REGIONS_JSON")
    if raw_json:
        try:
            out = {k.upper(): list(v) for k, v in json.loads(raw_json).items()}
        except json.JSONDecodeError:
            out = {}
    for region, env in _PROXY_REGION_ENV.items():
        vals = _list_env(env)
        if vals:
            out[region] = vals
    return out


def _exchange_rates() -> Dict[str, float]:
    """Static FX table (approx mid-rates). Override wholesale with
    EXCHANGE_RATES_JSON='{"EUR": 1.08, ...}'. Live rates are a roadmap item —
    the timestamp is recorded so stale conversions are visible."""
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
    app_name: str = "AI Competitor Research Tool"
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    # ---- Scraping ----
    firecrawl_api_key: str | None = os.getenv("FIRECRAWL_API_KEY") or None
    firecrawl_base_url: str = os.getenv("FIRECRAWL_BASE_URL", "https://api.firecrawl.dev/v1")
    proxy_urls: List[str] = field(default_factory=lambda: _list_env("PROXY_URLS"))
    proxy_regions: Dict[str, List[str]] = field(default_factory=_region_proxies)
    proxy_verify: bool = field(default_factory=lambda: _bool_env("PROXY_VERIFY", False))

    # ---- Resilience ----
    max_retries: int = int(os.getenv("MAX_RETRIES", "3"))
    backoff_base_seconds: float = float(os.getenv("BACKOFF_BASE_SECONDS", "1.5"))
    request_timeout_seconds: int = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))
    rate_limit_min_interval: float = float(os.getenv("RATE_LIMIT_MIN_INTERVAL", "1.0"))
    price_drop_alert_pct: float = float(os.getenv("PRICE_DROP_ALERT_PCT", "10.0"))
    min_scrape_success_rate: float = float(os.getenv("MIN_SCRAPE_SUCCESS_RATE", "0.8"))

    # ---- LLM ----
    llm_provider: str = os.getenv("LLM_PROVIDER", "auto").strip().lower()
    groq_api_key: str | None = os.getenv("GROQ_API_KEY") or None
    groq_model: str = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
    openai_api_key: str | None = os.getenv("OPENAI_API_KEY") or None
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    anthropic_api_key: str | None = os.getenv("ANTHROPIC_API_KEY") or None
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-3-5-haiku-latest")
    extraction_max_attempts: int = int(os.getenv("EXTRACTION_MAX_ATTEMPTS", "3"))

    # ---- FX ----
    exchange_rates: Dict[str, float] = field(default_factory=_exchange_rates)

    # ---- Storage ----
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///data/competitor.db")
    vector_store_dir: str = os.getenv("VECTOR_STORE_DIR", "data/vectors")
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
    embedding_dimensions: int = int(os.getenv("EMBEDDING_DIMENSIONS", "1536"))

    # ---- API security / frontend ----
    api_key: str | None = os.getenv("API_KEY") or None
    api_key_required: bool = field(default_factory=lambda: _bool_env("API_KEY_REQUIRED", True))
    cors_origins: List[str] = field(default_factory=lambda: _list_env("CORS_ORIGINS") or ["http://localhost:5173"])
    research_timeout_seconds: int = int(os.getenv("RESEARCH_TIMEOUT_SECONDS", "1800"))
    research_rate_limit_per_minute: int = int(os.getenv("RESEARCH_RATE_LIMIT_PER_MINUTE", "5"))

    # ---- Output ----
    report_output_dir: str = os.getenv("REPORT_OUTPUT_DIR", "data/reports")
    slack_webhook_url: str | None = os.getenv("SLACK_WEBHOOK_URL") or None
    alert_email_to: str | None = os.getenv("ALERT_EMAIL_TO") or None
    alert_email_from: str | None = os.getenv("ALERT_EMAIL_FROM") or None
    smtp_host: str | None = os.getenv("SMTP_HOST") or None
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_user: str | None = os.getenv("SMTP_USER") or None
    smtp_password: str | None = os.getenv("SMTP_PASSWORD") or None

    # ---- Scheduler ----
    scheduler_autostart: bool = field(default_factory=lambda: _bool_env("SCHEDULER_AUTOSTART", False))
    scheduler_poll_seconds: int = int(os.getenv("SCHEDULER_POLL_SECONDS", "60"))

    # ---- Derived ----
    @property
    def demo_mode(self) -> bool:
        return not (self.groq_api_key or self.openai_api_key or self.anthropic_api_key)

    def ensure_dirs(self) -> None:
        import os
        for d in (self.vector_store_dir, self.report_output_dir):
            os.makedirs(d, exist_ok=True)


def get_settings() -> Settings:
    try:
        from dotenv import load_dotenv  # optional convenience
        load_dotenv()
    except ImportError:
        pass
    return Settings()


settings = get_settings()
