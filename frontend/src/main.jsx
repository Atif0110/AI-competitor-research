import React, { useEffect, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './styles.css';

const API = import.meta.env.VITE_API_URL || 'http://localhost:8000';

async function api(path, options = {}) {
  const key = localStorage.getItem('acr_api_key') || '';
  const headers = { ...(options.body ? {'Content-Type':'application/json'} : {}), ...(key ? {'X-API-Key': key} : {}), ...(options.headers || {}) };
  const res = await fetch(`${API}${path}`, {...options, headers});
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) throw new Error(data?.detail || `Request failed (${res.status})`);
  return data;
}

const money = (n) => n == null ? '—' : `$${Number(n).toFixed(2)}`;
const pct = (n) => n == null ? '—' : `${(Number(n) * 100).toFixed(1)}%`;
const short = (s, n=54) => s?.length > n ? `${s.slice(0,n)}…` : s;

function App() {
  const [page, setPage] = useState('dashboard');
  const [health, setHealth] = useState(null);
  const [toast, setToast] = useState('');
  const [apiKey, setApiKey] = useState(localStorage.getItem('acr_api_key') || '');
  const [job, setJob] = useState(null);
  const [runId, setRunId] = useState(null);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => { api('/health').then(setHealth).catch(e => setToast(e.message)); }, [refresh]);
  useEffect(() => { if (!job || ['completed','failed'].includes(job.status)) return; const id = setInterval(() => api(`/research/jobs/${job.job_id}`).then(setJob).catch(e => setToast(e.message)), 1200); return () => clearInterval(id); }, [job]);
  useEffect(() => { if (toast) { const id=setTimeout(()=>setToast(''), 4500); return()=>clearTimeout(id); } }, [toast]);

  const startRun = async (target) => {
    try {
      const r = await api('/research/jobs', {method:'POST', body:JSON.stringify(target)});
      setJob(r); setRunId(r.job_id); setPage('run');
    } catch(e) { setToast(e.message); }
  };

  const nav = [
    ['dashboard','Overview'], ['run','New research'], ['offers','Offers'], ['insights','Insights'], ['ask','Ask'], ['reports','Reports']
  ];
  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark">AI</div><div><strong>Competitor</strong><span>Research</span></div></div>
      <div className="eyebrow">INTELLIGENCE CONSOLE</div>
      <nav>{nav.map(([id,label]) => <button key={id} className={page===id?'active':''} onClick={()=>setPage(id)}><span className="nav-dot" />{label}</button>)}</nav>
      <div className="sidebar-bottom">
        <div className="status-row"><span className={health?.status==='ok'?'status-dot live':'status-dot'} /> API {health?.status==='ok'?'online':'offline'}</div>
        <div className="provider">Provider <b>{health?.active_llm_provider || '—'}</b></div>
        <button className="settings" onClick={()=>{const k=prompt('API key', apiKey); if(k!==null){setApiKey(k);localStorage.setItem('acr_api_key',k);setRefresh(x=>x+1)}}}>API key</button>
      </div>
    </aside>
    <main className="main">
      <header className="topbar"><div><div className="eyebrow">COMPETITIVE INTELLIGENCE</div><h1>{nav.find(x=>x[0]===page)?.[1] || 'Overview'}</h1></div><div className="top-actions"><span className="mode-pill">{health?.mode || 'unknown'} mode</span><button className="primary" onClick={()=>setPage('run')}>+ New run</button></div></header>
      {page==='dashboard' && <Dashboard setPage={setPage} runId={runId} refresh={refresh}/>} 
      {page==='run' && <RunPage startRun={startRun} job={job} runId={runId} setPage={setPage}/>} 
      {page==='offers' && <Offers runId={runId}/>} 
      {page==='insights' && <Insights runId={runId}/>} 
      {page==='ask' && <Ask runId={runId}/>} 
      {page==='reports' && <Reports/>}
      {toast && <div className="toast">{toast}</div>}
    </main>
  </div>
}

function Dashboard({setPage, runId, refresh}) {
  const [agg,setAgg]=useState(null), [runs,setRuns]=useState([]), [events,setEvents]=useState([]);
  useEffect(()=>{Promise.all([api('/metrics/aggregate'),api('/metrics?limit=6'),runId?api(`/insights/events?run_id=${runId}`):Promise.resolve([])]).then(([a,r,e])=>{setAgg(a);setRuns(r);setEvents(e)}).catch(()=>{});},[runId,refresh]);
  const latest=agg?.latest_run;
  return <>
    <section className="hero"><div><div className="eyebrow">LATEST SIGNAL</div><h2>{latest?.target_company || 'Run your first competitive scan'}</h2><p>{latest ? `Last run ${latest.run_id} · ${latest.mode} · ${latest.started_at}` : 'Discover products, compare regional prices and turn customer reviews into evidence-backed intelligence.'}</p></div><button className="ghost" onClick={()=>setPage('run')}>Launch research →</button></section>
    <div className="kpi-grid">{[['Runs',agg?.runs||0],['Avg scrape',pct(agg?.avg_scrape)],['Avg extraction',pct(agg?.avg_extract)],['Products tracked',agg?.total_products||0]].map(([l,v])=><div className="card kpi" key={l}><span>{l}</span><strong>{v}</strong></div>)}</div>
    <div className="grid-2">
      <section className="card"><div className="section-head"><div><div className="eyebrow">RECENT RUNS</div><h3>Research history</h3></div><button className="link-btn" onClick={()=>setPage('reports')}>View all</button></div><div className="run-list">{runs.length?runs.map(r=><div className="run-item" key={r.run_id}><div className="run-icon">R</div><div className="run-main"><b>{r.target_company}</b><span>{r.run_id} · {r.mode}</span></div><div className="run-metric">{pct(r.scraping_success_rate)}<small>scrape</small></div><div className="run-metric">{pct(r.extraction_accuracy)}<small>extract</small></div></div>):<Empty text="No runs yet. Launch a research run to populate the console."/>}</div></section>
      <section className="card"><div className="section-head"><div><div className="eyebrow">ALERTS</div><h3>Competitive signals</h3></div></div>{events.length?events.slice(0,6).map((e,i)=><div className="alert" key={i}><span className={`severity ${e.severity||'info'}`}></span><div><b>{e.kind?.replaceAll('_',' ')}</b><p>{e.message}</p></div></div>):<Empty text="Run-specific alerts will appear here after analysis."/>}</section>
    </div>
  </>
}

function RunPage({startRun,job,runId,setPage}) {
  const [company,setCompany]=useState('Acme Audio'),[website,setWebsite]=useState('https://example.com'),[competitors,setCompetitors]=useState('Competitor One|https://example.org'),[regions,setRegions]=useState('US,UK,IN'),[products,setProducts]=useState('');
  const submit=e=>{e.preventDefault(); const comps=competitors.split('\n').filter(Boolean).map(line=>{const [name,site]=line.split('|').map(x=>x.trim());return {name,website:site||'https://example.com'}}); startRun({company,website,competitors:comps,regions:regions.split(',').map(x=>x.trim().toUpperCase()).filter(Boolean),focus_products:products.split(',').map(x=>x.trim()).filter(Boolean)});};
  const events=job?.events||[];
  return <div className="grid-2 run-layout"><section className="card form-card"><div className="eyebrow">RESEARCH CONFIGURATION</div><h2>Launch a regional scan</h2><p className="muted">The API will discover pages, fan them out by region, scrape evidence, extract structured offers and persist the run.</p><form onSubmit={submit}>
    <label>Target company<input value={company} onChange={e=>setCompany(e.target.value)} required/></label>
    <label>Target website<input type="url" value={website} onChange={e=>setWebsite(e.target.value)} required/></label>
    <label>Competitors <span className="hint">one per line: Name|https://site.com</span><textarea value={competitors} onChange={e=>setCompetitors(e.target.value)} rows="4" /></label>
    <div className="form-row"><label>Regions<input value={regions} onChange={e=>setRegions(e.target.value)} /></label><label>Focus products<input value={products} onChange={e=>setProducts(e.target.value)} placeholder="optional, comma separated" /></label></div>
    <button className="primary wide" disabled={job?.status==='queued'||job?.status==='running'}>{job?.status==='running'?'Research running…':'Start research'}</button>
  </form></section>
  <section className="card console"><div className="section-head"><div><div className="eyebrow">LIVE RUN CONSOLE</div><h3>{runId || 'Waiting for a run'}</h3></div>{job?.status==='completed'&&<button className="ghost" onClick={()=>setPage('offers')}>Explore offers →</button>}</div>{job?<div className="log">{events.length?events.map((e,i)=><div className="log-line" key={i}><span className="check">✓</span><span>{e.event.replaceAll('_',' ')}</span><small>{e.region||e.product||e.urls_discovered||e.pages_scraped||''}</small></div>):<div className="spinner-row"><span className="spinner"/>Initializing pipeline…</div>}{job.current_event?.event==='scrape_started'&&<div className="current">→ Scraping {job.current_event.region} · {short(job.current_event.url,70)}</div>}{job.status==='completed'&&<div className="success-box">Run complete. {job.result?.pages_scraped||0} pages scraped, {job.result?.extractions_ok||0} offers extracted.</div>}{job.status==='failed'&&<div className="error-box">{job.error}</div>}</div>:<Empty text="Start a run and this panel will show discovery, region fan-out, scraping and extraction events."/>}</section></div>
}

function Offers({runId}) {
  const [data,setData]=useState([]),[region,setRegion]=useState(''),[product,setProduct]=useState('');
  const load=()=>api(`/offers?limit=500${runId?`&run_id=${runId}`:''}${region?`&region=${region}`:''}${product?`&product=${encodeURIComponent(product)}`:''}`).then(setData).catch(()=>{});
  useEffect(load,[runId]);
  return <section className="card"><div className="section-head"><div><div className="eyebrow">EVIDENCE EXPLORER</div><h2>Offers</h2></div><button className="ghost" onClick={load}>Refresh</button></div><div className="filters"><input placeholder="Filter product" value={product} onChange={e=>setProduct(e.target.value)} onKeyDown={e=>e.key==='Enter'&&load()}/><select value={region} onChange={e=>{setRegion(e.target.value);setTimeout(load,0)}}><option value="">All regions</option>{['US','UK','IN','EU','JP','CA','AU','SG','BR'].map(r=><option key={r}>{r}</option>)}</select></div><div className="table-wrap"><table><thead><tr><th>Product</th><th>Competitor</th><th>Region</th><th>Price</th><th>USD</th><th>Availability</th><th>Confidence</th><th>Source</th></tr></thead><tbody>{data.map((o,i)=><tr key={o.offer_key||i}><td><b>{o.product_name}</b><small>{o.brand||''}</small></td><td>{o.competitor||'—'}</td><td><span className="tag">{o.region}</span></td><td>{o.price} {o.currency}</td><td><b>{money(o.normalized_price_usd)}</b></td><td>{String(o.availability).replaceAll('_',' ')}</td><td>{o.extraction_confidence!=null?pct(o.extraction_confidence):'—'}</td><td><a href={o.url} target="_blank" rel="noreferrer">Open ↗</a></td></tr>)}{!data.length&&<tr><td colSpan="8"><Empty text="No offers found for this filter."/></td></tr>}</tbody></table></div></section>
}

function Insights({runId}) {
  const [undercut,setUndercut]=useState([]),[moves,setMoves]=useState([]),[regional,setRegional]=useState([]);
  useEffect(()=>{Promise.all([api(`/insights/undercut${runId?`?run_id=${runId}`:''}`),api(`/insights/price-moves?days_back=30${runId?`&run_id=${runId}`:''}`),api(`/insights/regional${runId?`?run_id=${runId}`:''}`)]).then(([u,m,r])=>{setUndercut(u);setMoves(m);setRegional(r)}).catch(()=>{});},[runId]);
  return <div className="stack"><div className="insight-grid"><section className="card"><div className="eyebrow">UNDERCUTS</div><h3>Price pressure</h3>{undercut.slice(0,8).map((x,i)=><div className="signal-row" key={i}><div><b>{x.product_name}</b><span>{x.competitor||'competitor'} · {x.region}</span></div><strong>{x.undercut_pct!=null?`${Number(x.undercut_pct).toFixed(1)}%`:''}</strong></div>)}{!undercut.length&&<Empty text="No undercut signals for this run."/>}</section><section className="card"><div className="eyebrow">PRICE MOVES</div><h3>Recent changes</h3>{moves.slice(0,8).map((x,i)=><div className="signal-row" key={i}><div><b>{x.product_name}</b><span>{x.competitor||'competitor'} · {x.region}</span></div><strong>{x.change_pct!=null?`${Number(x.change_pct).toFixed(1)}%`:''}</strong></div>)}{!moves.length&&<Empty text="No historical price moves yet."/>}</section></div><section className="card"><div className="eyebrow">REGIONAL SNAPSHOT</div><h3>Market differences</h3><pre className="json-view">{JSON.stringify(regional,null,2)}</pre></section></div>
}

function Ask({runId}) {
  const [q,setQ]=useState('What are customers complaining about most?'),[answer,setAnswer]=useState(null),[busy,setBusy]=useState(false);
  const ask=async e=>{e?.preventDefault();setBusy(true);try{setAnswer(await api('/insights/reviews/query',{method:'POST',body:JSON.stringify({question:q,run_id:runId||null,n:8})}));}catch(err){setAnswer({answer:err.message,sources:[]})}finally{setBusy(false)}};
  return <section className="card ask-card"><div className="eyebrow">EVIDENCE-BACKED RAG</div><h2>Ask the review corpus</h2><p className="muted">Answers stay scoped to retrieved reviews and expose citations instead of inventing sources.</p><form onSubmit={ask} className="ask-form"><textarea value={q} onChange={e=>setQ(e.target.value)} rows="3"/><button className="primary" disabled={busy}>{busy?'Searching…':'Ask question'}</button></form>{answer&&<div className="answer"><div className="answer-label">ANSWER</div><p>{answer.answer}</p><div className="sources">{(answer.sources||[]).map((s,i)=><a key={i} href={s.source_url} target="_blank" rel="noreferrer"><span>[{i+1}]</span> {short(s.review_text||s.source_url,90)}</a>)}</div></div>}</section>
}

function Reports() {
  const [reports,setReports]=useState([]); useEffect(()=>{api('/reports').then(setReports).catch(()=>{})},[]);
  return <section className="card"><div className="section-head"><div><div className="eyebrow">REPORTS & METRICS</div><h2>Generated reports</h2></div></div>{reports.length?reports.map(r=><div className="report-row" key={r}><div className="file-icon">PDF</div><div><b>{r}</b><span>Generated report</span></div><a className="ghost" href={`${API}/reports/${r.replace(/^report_|\.pdf$/g,'')}`} target="_blank" rel="noreferrer">Open</a></div>):<Empty text="No PDF reports yet. Generate one from a completed run with scripts/generate_report.py."/>}</section>
}
function Empty({text}) { return <div className="empty"><span>○</span>{text}</div> }

createRoot(document.getElementById('root')).render(<App />);
