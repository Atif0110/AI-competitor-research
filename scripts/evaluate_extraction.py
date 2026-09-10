"""Evaluate structured extraction against hand-labelled real pages.

Input JSON shape is documented by eval/ground_truth.template.json.
The script reports exact field accuracy and numeric price tolerance for each
configured provider, plus a macro average across fields.

Example:
  python scripts/evaluate_extraction.py --dataset eval/ground_truth.json --out eval/artifacts/benchmark.json
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import settings
from app.llm.client import LLMClient
from app.llm.extractor import StructuredExtractor
from app.schemas import Region

FIELDS=('product_name','price','currency','availability','region','seller')
PROVIDERS=('anthropic','openai','groq')

def eq(field, got, expected):
    if field == 'price':
        try: return abs(float(got)-float(expected)) <= max(0.01, abs(float(expected))*0.005)
        except: return False
    if field == 'seller':
        if expected in (None,''): return got in (None,'')
        return str(got or '').strip().lower() == str(expected).strip().lower()
    return str(getattr(got,'value',got)).strip().lower() == str(expected).strip().lower()

def run_provider(provider: str, rows: list[dict]) -> dict:
    key_attr=f'{provider}_api_key'
    if not getattr(settings,key_attr,None): return {'status':'skipped','reason':f'{key_attr.upper()} not configured'}
    settings.llm_provider=provider
    extractor=StructuredExtractor(LLMClient())
    totals={f:0 for f in FIELDS}; correct={f:0 for f in FIELDS}; errors=[]
    for row in rows:
        markdown=Path(row['markdown_path']).read_text()
        expected=row['expected']; region=Region(expected['region'])
        result=extractor.extract(markdown,row['url'],expected_region=region)
        if not result.ok:
            errors.append({'id':row['id'],'error':result.error}); continue
        offer=result.offer
        for f in FIELDS:
            totals[f]+=1
            if eq(f,getattr(offer,f,None),expected.get(f)): correct[f]+=1
    scores={f:(correct[f]/totals[f] if totals[f] else 0.0) for f in FIELDS}
    return {'status':'ok','pages':len(rows),'field_accuracy':scores,'macro_accuracy':sum(scores.values())/len(scores),'correct':correct,'evaluated':totals,'errors':errors,'provider':provider}

def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument('--dataset',required=True); p.add_argument('--out',default='eval/artifacts/benchmark.json'); args=p.parse_args(argv)
    rows=json.loads(Path(args.dataset).read_text())
    results={provider:run_provider(provider,rows) for provider in PROVIDERS}
    payload={'dataset':args.dataset,'pages':len(rows),'fields':list(FIELDS),'results':results}
    out=Path(args.out); out.parent.mkdir(parents=True,exist_ok=True); out.write_text(json.dumps(payload,indent=2))
    print(json.dumps(payload,indent=2))

if __name__=='__main__': main()
