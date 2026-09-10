"""Run a small real-internet smoke test and save auditable artifacts.

Usage:
  python scripts/run_live_smoke.py --config config/live_smoke.json

The script refuses to run in demo mode. It writes a manifest plus a copy of
run metrics/evidence metadata under data/live_runs/<run_id>/.
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import settings
from app.orchestrator import Pipeline
from app.schemas import CompetitorTarget
from app.storage.db import OfferStore


def main(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument('--config', default='config/live_smoke.example.json')
    args=p.parse_args(argv)
    if settings.demo_mode:
        raise SystemExit('Refusing live smoke: no LLM API key is configured. Add OPENAI_API_KEY or ANTHROPIC_API_KEY (Groq is also supported).')
    cfg=json.loads(Path(args.config).read_text())
    target=CompetitorTarget.model_validate(cfg)
    store=OfferStore()
    result=Pipeline(store=store).run(target)
    out=Path('data/live_runs')/result.run_id
    out.mkdir(parents=True, exist_ok=True)
    manifest={
        'run_id': result.run_id,
        'run_date': result.run_date,
        'mode': result.mode,
        'target': target.model_dump(mode='json'),
        'metrics': result.metrics,
        'errors': result.errors,
        'evidence': store.evidence_for_run(result.run_id, limit=200),
    }
    (out/'manifest.json').write_text(json.dumps(manifest, indent=2, default=str))
    (out/'result.json').write_text(json.dumps(result.model_dump(mode='json'), indent=2))
    print(json.dumps({'run_id': result.run_id, 'output_dir': str(out), 'metrics': result.metrics}, indent=2))
    if result.mode != 'live': raise SystemExit('Smoke test did not run in live mode.')
    if result.pages_scraped == 0: raise SystemExit('Live smoke completed but scraped zero pages. Inspect manifest/errors.')

if __name__ == '__main__': main()
