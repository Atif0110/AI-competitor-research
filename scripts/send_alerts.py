"""Send Slack/e-mail alerts for a completed run (used by cron / CI / manual).

Usage: python scripts/send_alerts.py --run-id <id> --company "Acme Audio" --highlights "price drop on Pro X"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.output.alerts import send_alerts  # noqa: E402
from app.schemas import CompetitorTarget, PipelineResult  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Alert Slack/email about a completed run")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--company", required=True)
    parser.add_argument("--highlights", nargs="*", default=[])
    args = parser.parse_args()

    result = PipelineResult(
        target=CompetitorTarget(company=args.company, website="https://example.com"),
        run_id=args.run_id, run_date=__import__("time").strftime("%Y-%m-%d"),
        mode="live", pages_scraped=0, extractions_ok=0, extractions_failed=0,
        retries_used=0, duration_s=0.0,
    )
    send_alerts(result, args.highlights)
    print("Alerts sent (or skipped — configure SLACK_WEBHOOK_URL / SMTP in .env).")


if __name__ == "__main__":
    main()
