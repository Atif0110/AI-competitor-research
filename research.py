"""Research entry point — the `python research.py` demo (Priority list).

Loads config/example_targets.json (target + competitors + regions + products),
runs the full pipeline per target, and prints a defensible summary line where
every number comes from the actual run metrics table.

Usage:
    python research.py                       # demo mode, no keys needed
    python research.py --config my.json      # your targets + competitors
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import settings  # noqa: E402
from app.orchestrator import Pipeline  # noqa: E402
from app.schemas import Competitor, CompetitorTarget  # noqa: E402

DEFAULT_CONFIG = Path(__file__).parent / "config" / "example_targets.json"


def load_targets(config_path: Path) -> list[CompetitorTarget]:
    raw = json.loads(Path(config_path).read_text())
    targets = []
    for t in raw["targets"]:
        targets.append(CompetitorTarget(
            company=t["company"], website=t["website"],
            regions=t.get("regions", ["US"]), focus_products=t.get("focus_products", []),
            competitors=[Competitor(**c) for c in t.get("competitors", [])],
        ))
    return targets


def main() -> None:
    parser = argparse.ArgumentParser(description="Run competitor research (demo mode by default)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    args = parser.parse_args()

    targets = load_targets(Path(args.config))
    pipeline = Pipeline()
    settings.ensure_dirs()
    rows = []
    for target in targets:
        print(f"\n=== Researching {target.company} "
              f"(competitors: {[c.name for c in target.competitors] or '—'}) ===")
        result = pipeline.run(target)
        m = result.metrics
        rows.append(m)
        print(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False)[:1200])
        for e in result.events or []:
            print(f"  ⚡ {e['message']}")

    print("\n=== SUMMARY (all numbers from run_metrics table) ===")
    for m in rows:
        print(
            f"{m['target_company']:<12} {m['competitors']} competitors · {m['regions']} markets · "
            f"{m['products']} products · {m['urls_succeeded']} pages · "
            f"scrape {m['scraping_success_rate'] * 100:.1f}% · "
            f"extraction {m['extraction_accuracy'] * 100:.1f}% · "
            f"{m['runtime_s']:.1f}s · providers={m['providers_used']} scrapers={m['scrapers_used']}"
        )
    agg = pipeline.store.aggregate_metrics()
    if agg.get("runs"):
        print(
            f"\nAggregate: {agg['runs']} runs · avg scrape {agg['avg_scrape'] * 100:.1f}% · "
            f"avg extraction {agg['avg_extract'] * 100:.1f}% · avg runtime {agg['avg_runtime']:.1f}s"
        )


if __name__ == "__main__":
    main()
