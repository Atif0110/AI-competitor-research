"""Static readiness audit for the 9.5 portfolio target.

This script checks that the engineering pieces are present. It deliberately
cannot mark the live-run and human-labelled benchmark boxes complete because
those require real external evidence.
"""
from __future__ import annotations
from pathlib import Path
import subprocess, sys

ROOT=Path(__file__).resolve().parents[1]
checks={
    'FastAPI API': ROOT/'app/api.py',
    'React frontend': ROOT/'frontend/src/main.jsx',
    'PostgreSQL storage': ROOT/'app/storage/db.py',
    'pgvector review path': ROOT/'app/storage/vector.py',
    'Provider routing': ROOT/'app/llm/client.py',
    'Live smoke harness': ROOT/'scripts/run_live_smoke.py',
    'Extraction benchmark': ROOT/'scripts/evaluate_extraction.py',
    'CI workflow': ROOT/'.github/workflows/ci.yml',
    'API key generator': ROOT/'scripts/generate_api_key.py',
}
print('9.5 READINESS AUDIT')
print('='*60)
failed=[]
for name,path in checks.items():
    ok=path.exists()
    print(f"{'PASS' if ok else 'FAIL':4}  {name:28} {path.relative_to(ROOT)}")
    if not ok: failed.append(name)
print('-'*60)
print('HUMAN EVIDENCE REQUIRED:')
for item in ('Real internet run + saved manifest','Real GPT/Claude fallback verification','15–20 page hand-labelled benchmark','Frontend screenshots','60–90 second demo video','README populated with real dated metrics'):
    print(f'PEND  {item}')
print('-'*60)
try:
    r=subprocess.run([sys.executable,'-m','pytest','-q'],cwd=ROOT,text=True,capture_output=True,timeout=180)
    print('PASS  pytest' if r.returncode==0 else 'FAIL  pytest')
    print(r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr.strip())
    if r.returncode: failed.append('pytest')
except Exception as exc:
    print(f'FAIL  pytest ({exc})'); failed.append('pytest')
print('='*60)
if failed: sys.exit(1)
print('Engineering readiness: PASS. Final 9.5 rating still depends on the human evidence above.')
