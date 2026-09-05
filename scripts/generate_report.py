"""Generate a PDF report from REAL run metrics — never fabricated numbers.

Usage:
    python scripts/generate_report.py --run-id <run_id>
    python scripts/generate_report.py                 # most recent run

Exits 1 with an error if the run does not exist in run_metrics.
"""
from __future__ import annotations

import argparse
import sys
import time as time_mod
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.analysis.insights import InsightsEngine  # noqa: E402
from app.config import settings  # noqa: E402
from app.llm.client import LLMClient  # noqa: E402
from app.output.report import ReportBuilder  # noqa: E402
from app.schemas import CompetitorTarget, PipelineResult, RunMetrics  # noqa: E402
from app.storage.db import OfferStore  # noqa: E402
from app.storage.vector import ReviewStore  # noqa: E402

settings.ensure_dirs()


def main(argv: list | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build a PDF report from stored run metrics")
    parser.add_argument("--run-id", default=None, help="run id (default: most recent run)")
    args = parser.parse_args(argv)

    store = OfferStore()
    reviews = ReviewStore()
    insights = InsightsEngine(store, reviews, LLMClient())

    runs = store.recent_runs(limit=1000)
    row = next((r for r in runs if r["run_id"] == args.run_id), None) if args.run_id \
        else (runs[0] if runs else None)
    if row is None:
        print(f"ERROR: run '{args.run_id or '(none found)'}' not found in run_metrics. "
              "Run research.py first.", file=sys.stderr)
        sys.exit(1)

    metrics = RunMetrics.model_validate(row)
    target = CompetitorTarget(company=metrics.target_company, website="https://example.com")
    result = PipelineResult(
        target=target, run_id=metrics.run_id, run_date=time_mod.strftime("%Y-%m-%d"),
        mode=metrics.mode,
        pages_scraped=metrics.urls_succeeded, extractions_ok=metrics.extraction_successes,
        extractions_failed=metrics.extraction_failures, retries_used=metrics.retries_used,
        duration_s=metrics.runtime_s, errors=metrics.errors,
        competitors=metrics.competitors, discovered_urls=metrics.urls_discovered,
        events=[e.model_dump(mode="json") for e in insights.detect_events(metrics.run_id)],
        metrics=metrics.model_dump(mode="json"),
    )
    path = ReportBuilder(insights).build(result)
    print(f"Report written to {path}")


if __name__ == "__main__":
    main()
