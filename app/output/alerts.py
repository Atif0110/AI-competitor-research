"""Event-driven alerts (Priority 14).

Alerts are triggered by events, not just "run finished":
  ▼ price drop >threshold   ⇄ undercut leader change   ! low scrape success
Formats messages for Slack + SMTP. Failures are logged, never fatal.
"""
from __future__ import annotations

import logging
from typing import List

from app.config import settings
from app.schemas import EventAlert, PipelineResult

logger = logging.getLogger(__name__)

_ICONS = {
    "price_drop": "🔴",
    "price_increase": "🟠",
    "undercut_change": "⇄",
    "new_product": "🆕",
    "low_scrape_success": "🚨",
}


def format_event_message(e: EventAlert) -> str:
    return f"{_ICONS.get(e.kind, '•')} {e.kind.replace('_', ' ')} — {e.message}"


def send_alerts(result: PipelineResult, highlights: List[str]) -> None:
    """Backwards-compatible wrapper used by scripts/send_alerts.py."""
    if settings.slack_webhook_url:
        try:
            _slack(result, highlights)
        except Exception as e:
            logger.warning("slack alert failed: %s", e)
    if settings.alert_email_to and settings.smtp_host:
        try:
            _email(result, highlights)
        except Exception as e:
            logger.warning("email alert failed: %s", e)


def send_event_alerts(result: PipelineResult, events: List[EventAlert]) -> None:
    if not events:
        return
    lines = [format_event_message(e) for e in events]
    send_alerts(result, lines)


def _slack(result: PipelineResult, highlights: List[str]) -> None:
    import requests

    lines = "\n".join(f"• {h}" for h in highlights[:8])
    requests.post(settings.slack_webhook_url, json={
        "text": (
            f"*Competitive intelligence — {result.target.company}*\n"
            f"Run `{result.run_id}` · Mode {result.mode} · "
            f"Pages {result.pages_scraped} · OK {result.extractions_ok} · Failed {result.extractions_failed}\n"
            f"{lines or '_no events_'}"
        )
    }, timeout=10).raise_for_status()


def _email(result: PipelineResult, highlights: List[str]) -> None:
    import smtplib
    from email.mime.text import MIMEText

    body = "\n".join([
        f"Run: {result.run_id} ({result.mode})",
        f"Target: {result.target.company}",
        f"Pages scraped: {result.pages_scraped}",
        f"Extractions: {result.extractions_ok} ok / {result.extractions_failed} failed",
        "Events:",
        *(f"- {h}" for h in highlights[:8]),
    ])
    msg = MIMEText(body)
    msg["Subject"] = f"ⓘ Competitor intel: {result.target.company}"
    msg["From"] = settings.alert_email_from
    msg["To"] = settings.alert_email_to
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as s:
        if settings.smtp_user:
            s.starttls()
            s.login(settings.smtp_user, settings.smtp_password)
        s.send_message(msg)
