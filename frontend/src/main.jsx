import React, { useEffect, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './styles.css';
import './deep.css';
import './theme.css';

const API = import.meta.env.VITE_API_URL || 'http://localhost:8000';
const NAV = [
  ['dashboard', 'Overview', 'home'],
  ['run', 'Research', 'scan'],
  ['deep', 'Deep research', 'compass'],
  ['offers', 'Evidence', 'layers'],
  ['insights', 'Signals', 'spark'],
  ['ask', 'Ask AI', 'message'],
  ['chat', 'Chat', 'quote'],
  ['reports', 'Reports', 'file'],
];
const REGIONS = ['US', 'UK', 'IN', 'EU', 'JP', 'CA', 'AU', 'SG', 'BR'];

async function api(path, options = {}) {
  const key = localStorage.getItem('acr_api_key') || '';
  const headers = {
    ...(options.body ? { 'Content-Type': 'application/json' } : {}),
    ...(key ? { 'X-API-Key': key } : {}),
    ...(options.headers || {}),
  };
  let res;
  try {
    res = await fetch(`${API}${path}`, { ...options, headers });
  } catch (error) {
    throw new Error(`The research API could not be reached. Check the API service and its allowed frontend origin, then retry. (${API})`);
  }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (res.status === 401) throw new Error('Workspace API key required. Open “Workspace API key” and enter the API_KEY configured on Render — not your GROQ_API_KEY.');
  if (!res.ok) throw new Error(data?.detail || `Request failed (${res.status})`);
  return data;
}

const money = (n, currency = 'USD') => n == null ? '—' : `${currency === 'USD' ? '$' : ''}${Number(n).toFixed(2)}${currency !== 'USD' ? ` ${currency}` : ''}`;
const pct = (n) => n == null ? '—' : `${(Number(n) * 100).toFixed(1)}%`;
const short = (s, n = 64) => s?.length > n ? `${s.slice(0, n)}…` : (s || '—');
const pretty = (s) => String(s || '').replaceAll('_', ' ');

function Icon({ name, size = 18, stroke = 1.8 }) {
  const paths = {
    home: <><path d="m3 9 9-7 9 7"/><path d="M5 10v10h14V10"/><path d="M9 20v-6h6v6"/></>,
    scan: <><path d="M7 3H5a2 2 0 0 0-2 2v2"/><path d="M17 3h2a2 2 0 0 1 2 2v2"/><path d="M7 21H5a2 2 0 0 1-2-2v-2"/><path d="M17 21h2a2 2 0 0 0 2-2v-2"/><circle cx="12" cy="12" r="4"/></>,
    layers: <><path d="m12 2 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5"/><path d="m3 17 9 5 9-5"/></>,
    spark: <><path d="m12 3-1.2 4.8L6 9l4.8 1.2L12 15l1.2-4.8L18 9l-4.8-1.2L12 3Z"/><path d="m19 14-.7 2.3L16 17l2.3.7L19 20l.7-2.3L22 17l-2.3-.7L19 14Z"/></>,
    message: <><path d="M20 11.5a7.5 7.5 0 0 1-8 7.5 8.7 8.7 0 0 1-4-.9L4 20l1.1-3.4A7.3 7.3 0 0 1 4.5 12 7.5 7.5 0 0 1 12 4.5a7.5 7.5 0 0 1 8 7Z"/></>,
    file: <><path d="M6 2h9l4 4v16H6z"/><path d="M15 2v5h5"/><path d="M9 13h6M9 17h6"/></>,
    settings: <><path d="M12 15.2a3.2 3.2 0 1 0 0-6.4 3.2 3.2 0 0 0 0 6.4Z"/><path d="m19.4 15 .8 1.4-1.9 1.9-1.4-.8-1.5.6-.4 1.6h-2.7l-.4-1.6-1.5-.6-1.4.8-1.9-1.9.8-1.4-.6-1.5-1.6-.4v-2.7l1.6-.4.6-1.5-.8-1.4 1.9-1.9 1.4.8 1.5-.6.4-1.6H15l.4 1.6 1.5.6 1.4-.8 1.9 1.9-.8 1.4.6 1.5 1.6.4v2.7l-1.6.4-.6 1.5Z"/></>,
    plus: <><path d="M12 5v14M5 12h14"/></>,
    arrow: <><path d="M5 12h14"/><path d="m13 6 6 6-6 6"/></>,
    refresh: <><path d="M20 11a8 8 0 1 0 1 4"/><path d="M20 4v7h-7"/></>,
    check: <path d="m5 12 4 4L19 6"/>,
    external: <><path d="M14 4h6v6"/><path d="M20 4 11 13"/><path d="M18 13v5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h5"/></>,
    clock: <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></>,
    database: <><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v7c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 12v7c0 1.7 3.6 3 8 3s8-1.3 8-3v-7"/></>,
    menu: <><path d="M4 6h16M4 12h16M4 18h16"/></>,
    compass: <><circle cx="12" cy="12" r="9"/><path d="m15.5 8.5-2.2 4.8-4.8 2.2 2.2-4.8z"/></>,
    quote: <><path d="M9 7H6a2 2 0 0 0-2 2v3a2 2 0 0 0 2 2h1v3"/><path d="M18 7h-3a2 2 0 0 0-2 2v3a2 2 0 0 0 2 2h1v3"/></>,
    globe: <><circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a15 15 0 0 1 0 18a15 15 0 0 1 0-18Z"/></>,
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={stroke} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] || paths.spark}</svg>;
}

function App() {
  const [page, setPage] = useState('dashboard');
  const [health, setHealth] = useState(null);
  const [toast, setToast] = useState('');
  const [apiKey, setApiKey] = useState(localStorage.getItem('acr_api_key') || '');
  const [job, setJob] = useState(null);
  const [runId, setRunId] = useState(null);
  const [deepRun, setDeepRun] = useState(null);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [intro, setIntro] = useState(true);

  useEffect(() => {
    const id = setTimeout(() => setIntro(false), 760);
    return () => clearTimeout(id);
  }, []);

  const loadHealth = () => api('/health').then(setHealth).catch(e => { setHealth({ status: 'error', mode: 'unknown', error: e.message }); setToast(e.message); });
  useEffect(() => { loadHealth(); }, [refresh]);
  useEffect(() => {
    if (!job || ['completed', 'failed'].includes(job.status)) return;
    const id = setInterval(() => api(`/research/jobs/${job.job_id}`).then(setJob).catch(e => setToast(e.message)), 1200);
    return () => clearInterval(id);
  }, [job]);
  useEffect(() => {
    if (!toast) return;
    const id = setTimeout(() => setToast(''), 4500);
    return () => clearTimeout(id);
  }, [toast]);

  const startRun = async (target) => {
    try {
      const r = await api('/research/jobs', { method: 'POST', body: JSON.stringify(target) });
      setJob(r); setRunId(r.job_id); setPage('run'); setMobileOpen(false);
    } catch (e) { setToast(e.message); }
  };

  const title = NAV.find(x => x[0] === page)?.[1] || 'Overview';
  if (intro) return <IntroScreen />;
  return <div className="app-shell">
    <div className="ambient ambient-one"/><div className="ambient ambient-two"/>
    <aside className={`sidebar ${mobileOpen ? 'mobile-open' : ''}`}>
      <div className="brand-row">
        <div className="brand-mark"><span>CR</span><i/></div>
        <div className="brand-copy"><strong>Competitor</strong><span>Research</span></div>
        <button className="mobile-close" onClick={() => setMobileOpen(false)}>×</button>
      </div>
      <div className="workspace-pill"><span className="pulse-dot"/> Intelligence workspace</div>
      <div className="nav-label">WORKSPACE</div>
      <nav>{NAV.map(([id, label, icon]) => <button key={id} className={page === id ? 'active' : ''} onClick={() => { setPage(id); setMobileOpen(false); }}><span className="nav-icon"><Icon name={icon}/></span><span>{label}</span>{id === 'insights' && <em>live</em>}</button>)}</nav>
      <div className="sidebar-spacer"/>
      <div className="provider-card">
        <div className="provider-head"><span className="mini-orb"/><span>AI routing</span><span className="online-dot"/></div>
        <strong>{health?.active_llm_provider || (health?.status === 'error' ? 'Connect API' : 'Checking…')}</strong>
        <small>{health?.llm_provider_chain?.length ? health.llm_provider_chain.join(' → ') : health?.status === 'error' ? 'Add your app API key below' : 'Loading provider status…'}</small>
      </div>
      <div className="sidebar-footer">
        <div className="status-line"><span className={health?.status === 'ok' ? 'status-dot live' : 'status-dot'}/><span>API {health?.status === 'ok' ? 'online' : health?.status === 'error' ? 'check access' : 'checking'}</span><span className="footer-mode">{health?.status === 'ok' ? (health.mode || 'unknown') : '—'}</span></div>
        <button className="settings-link" onClick={() => { const k = prompt('Enter the workspace API_KEY configured on Render. Do not enter your GROQ_API_KEY here.', apiKey); if (k !== null) { setApiKey(k.trim()); localStorage.setItem('acr_api_key', k.trim()); setRefresh(x => x + 1); } }}><Icon name="settings" size={16}/> Workspace API key</button>
      </div>
    </aside>
    {mobileOpen && <button className="mobile-overlay" onClick={() => setMobileOpen(false)} aria-label="Close navigation"/>}
    <main className="main">
      <header className="topbar">
        <div className="top-left"><button className="menu-button" onClick={() => setMobileOpen(true)}><Icon name="menu"/></button><div><div className="crumb"><span>INTELLIGENCE</span><b>/</b><span>{title.toUpperCase()}</span></div><h1>{title}</h1></div></div>
        <div className="top-actions"><div className={`connection-pill ${health?.status === 'error' ? 'needs-access' : ''}`}><span className={health?.status === 'ok' ? 'status-dot live' : 'status-dot'}/>{health?.active_llm_provider || (health?.status === 'error' ? 'connect API' : 'checking')}<span className="divider"/> {health?.status === 'ok' ? (health.mode || 'unknown') : '—'}</div><button className="primary top-new" onClick={() => setPage('run')}><Icon name="plus" size={16}/> New research</button></div>
      </header>
      <div className="page-wrap">
        {page === 'dashboard' && <Dashboard setPage={setPage} runId={runId} refresh={refresh} health={health}/>}
        {page === 'run' && <RunPage startRun={startRun} job={job} runId={runId} setPage={setPage}/>}
        {page === 'deep' && <DeepResearch deepRun={deepRun} setDeepRun={setDeepRun} onChat={() => setPage('chat')}/>}
        {page === 'offers' && <Offers runId={runId}/>}
        {page === 'insights' && <Insights runId={runId}/>}
        {page === 'ask' && <Ask runId={runId}/>}
        {page === 'chat' && <ResearchChat runId={deepRun?.run_id || null} runTitle={deepRun?.plan?.subject || deepRun?.question}/>}
        {page === 'reports' && <Reports/>}
      </div>
      {toast && <div className="toast"><span className="toast-icon">!</span><span>{toast}</span><button onClick={() => setToast('')}>×</button></div>}
    </main>
  </div>;
}

function IntroScreen() {
  return <div className="intro-screen" aria-label="Loading competitor research workspace">
    <div className="intro-content">
      <div className="intro-mark">CR</div>
      <h1>Competitor Research</h1>
      <p>Preparing intelligence workspace</p>
      <div className="intro-line"><span/></div>
    </div>
  </div>;
}

function Dashboard({ setPage, runId, refresh, health }) {
  const [agg, setAgg] = useState(null), [runs, setRuns] = useState([]), [events, setEvents] = useState([]);
  const load = () => Promise.all([
    api('/metrics/aggregate'), api('/metrics?limit=8'), runId ? api(`/insights/events?run_id=${runId}`) : Promise.resolve([]),
  ]).then(([a, r, e]) => { setAgg(a); setRuns(r); setEvents(e); }).catch(() => {});
  useEffect(() => {
  load();
  }, [runId, refresh]);
  const latest = agg?.latest_run;
  const avgRuntime = agg?.avg_runtime ? `${Number(agg.avg_runtime).toFixed(0)}s` : '—';
  return <div className="page-stack">
    <section className="hero-panel reveal">
      <div className="hero-grid"/>
      <div className="hero-copy"><div className="eyebrow accent"><span className="spark-dot"/> COMPETITIVE INTELLIGENCE</div><h2>{latest?.target_company ? <>Signals for <span>{latest.target_company}</span></> : <>Know what your competitors are<br/><span>doing before everyone else.</span></>}</h2><p>{latest ? `Latest research run ${latest.run_id} completed in ${avgRuntime}. Explore evidence, pricing moves and market signals from the same run.` : 'Discover products, regional pricing and customer signals, then turn them into traceable intelligence with evidence attached to every observation.'}</p><div className="hero-actions"><button className="primary" onClick={() => setPage('run')}><Icon name="scan" size={16}/> Start research</button><button className="secondary" onClick={() => setPage('insights')}><Icon name="spark" size={16}/> View signals</button></div></div>
      <div className="hero-visual"><div className="hero-glow"/><div className="orbit-ring ring-one"/><div className="orbit-ring ring-two"/><div className="orbit-core"><span>ACR</span><small>INTELLIGENCE</small></div><div className="orbit-node n2">◎</div><div className="signal-float"><span className="signal-pulse"/> New market signal <b>detected</b></div></div>
      <div className="hero-foot"><span><b>Evidence first</b> Every run retains source context and timestamps.</span><span className="hero-foot-right"><span className={health?.status === 'ok' ? 'status-dot live' : 'status-dot'}/> {health?.status === 'ok' ? (health.mode === 'live' ? 'Live provider' : 'Deterministic demo') : 'Connect API for live status'}</span></div>
    </section>

    <div className="metric-grid reveal delay-1">
      <Metric icon="scan" label="Research runs" value={agg?.runs ?? 0} hint="Completed pipeline runs"/>
      <Metric icon="layers" label="Products tracked" value={agg?.total_products ?? 0} hint="Across stored observations"/>
      <Metric icon="spark" label="Extraction quality" value={pct(agg?.avg_extract)} hint="Average validation score"/>
      <Metric icon="clock" label="Avg. runtime" value={avgRuntime} hint="End-to-end research time"/>
    </div>

    <div className="content-grid reveal delay-2">
      <section className="surface large-surface"><SectionHead eyebrow="RESEARCH HISTORY" title="Runs" action="View all" onAction={() => setPage('reports')}/>{runs.length ? <div className="run-table"><div className="table-head"><span>Run</span><span>Mode</span><span>Scrape</span><span>Extract</span><span>Runtime</span></div>{runs.slice(0, 6).map(r => <div className="table-row" key={r.run_id}><div className="run-name"><span className="run-badge">{String(r.target_company || 'R').slice(0,1).toUpperCase()}</span><div><b>{r.target_company}</b><small>{r.run_id}</small></div></div><span className="mode-text">{r.mode}</span><strong>{pct(r.scraping_success_rate)}</strong><strong>{pct(r.extraction_accuracy)}</strong><span>{r.runtime_s ? `${Number(r.runtime_s).toFixed(0)}s` : '—'}</span></div>)}</div> : <Empty title="Your research history starts here" text="Launch a scan to populate the workspace with real runs." action="Start research" onAction={() => setPage('run')}/>}</section>
      <section className="surface"><SectionHead eyebrow="SIGNALS" title="What changed" action="Explore" onAction={() => setPage('insights')}/>{events.length ? <div className="signal-list">{events.slice(0, 5).map((e, i) => <Signal key={i} event={e}/>)}</div> : <Empty title="No new signals yet" text="Price moves, new products and undercut changes appear here after a run."/>}</section>
    </div>

    <section className="architecture-strip reveal delay-3"><div><div className="eyebrow">PIPELINE HEALTH</div><h3>From web evidence to decision-ready intelligence.</h3></div><div className="pipeline-flow">{['Discover', 'Scrape', 'Extract', 'Validate', 'Compare', 'Report'].map((x, i) => <React.Fragment key={x}><span className="pipeline-step"><i>{String(i + 1).padStart(2,'0')}</i>{x}</span>{i < 5 && <b>→</b>}</React.Fragment>)}</div></section>
  </div>;
}

function Metric({ icon, label, value, hint }) { return <div className="metric surface"><div className="metric-icon"><Icon name={icon}/></div><div><span>{label}</span><strong>{value}</strong><small>{hint}</small></div></div>; }
function SectionHead({ eyebrow, title, action, onAction }) { return <div className="section-head"><div><div className="eyebrow">{eyebrow}</div><h3>{title}</h3></div>{action && <button className="link-btn" onClick={onAction}>{action}<Icon name="arrow" size={15}/></button>}</div>; }
function Signal({ event }) { return <div className="signal-item"><span className={`signal-dot ${event.severity || 'info'}`}/><div><b>{pretty(event.kind)}</b><p>{short(event.message, 96)}</p><small>{event.region || 'Cross-region'}{event.competitor ? ` · ${event.competitor}` : ''}</small></div><Icon name="arrow" size={15}/></div>; }

function RunPage({ startRun, job, runId, setPage }) {
  const [company, setCompany] = useState('Acme Audio'), [website, setWebsite] = useState('https://example.com');
  const [competitors, setCompetitors] = useState('Competitor One|https://example.org');
  const [regions, setRegions] = useState(['US', 'UK', 'IN']), [products, setProducts] = useState('');
  const submit = e => { e.preventDefault(); const comps = competitors.split('\n').map(x => x.trim()).filter(Boolean).map(line => { const [name, site] = line.split('|').map(x => x.trim()); return { name, website: site || 'https://example.com' }; }); startRun({ company, website, competitors: comps, regions, focus_products: products.split(',').map(x => x.trim()).filter(Boolean) }); };
  const events = job?.events || [];
  const progress = job?.status === 'completed' ? 100 : job?.status === 'running' ? Math.min(92, 18 + events.length * 7) : 0;
  return <div className="page-stack">
    <div className="page-intro"><div><div className="eyebrow accent">RESEARCH WORKSPACE</div><h2>Launch a competitive scan.</h2><p>Configure once. The pipeline handles discovery, regional fan-out, scraping, structured extraction, validation and analysis.</p></div><div className="trust-chip"><span className="pulse-dot"/> Evidence-backed pipeline</div></div>
    <div className="research-grid">
      <section className="surface form-surface"><div className="form-title"><div className="step-number">01</div><div><div className="eyebrow">CONFIGURATION</div><h3>Research scope</h3></div></div><form onSubmit={submit}>
        <Field label="Target company" value={company} onChange={setCompany} placeholder="Company you want to benchmark"/>
        <Field label="Target website" value={website} onChange={setWebsite} placeholder="https://example.com" type="url"/>
        <label className="field"><span>Competitors <em>Name|website, one per line</em></span><textarea value={competitors} onChange={e => setCompetitors(e.target.value)} rows="5"/></label>
        <div className="field"><span>Regions <em>Select one or more</em></span><div className="region-grid">{REGIONS.map(r => <button type="button" key={r} className={regions.includes(r) ? 'region active' : 'region'} onClick={() => setRegions(v => v.includes(r) ? v.filter(x => x !== r) : [...v, r])}>{r}</button>)}</div></div>
        <Field label="Focus products" value={products} onChange={setProducts} placeholder="Optional · comma separated"/>
        <button className="primary wide" disabled={job?.status === 'queued' || job?.status === 'running'}><Icon name="scan" size={17}/>{job?.status === 'running' ? 'Research in progress…' : 'Run intelligence scan'}<span className="button-arrow"><Icon name="arrow" size={15}/></span></button>
      </form></section>
      <section className="surface run-surface"><div className="run-console-head"><div><div className="eyebrow">LIVE RUN CONSOLE</div><h3>{runId || 'Ready when you are'}</h3></div><StatusBadge status={job?.status || 'idle'}/></div>
        {job ? <><ResearchPipeline status={job.status} events={events}/><div className="progress-wrap"><div className="progress-meta"><span>{job.status === 'completed' ? 'Completed' : job.status === 'failed' ? 'Run failed' : 'Processing evidence'}</span><b>{progress}%</b></div><div className="progress"><span style={{width: `${progress}%`}}/></div></div><div className="console-log">{events.length ? events.map((e, i) => <div className="log-event" key={i}><span className="log-check"><Icon name="check" size={13}/></span><div><b>{pretty(e.event)}</b><small>{e.region || e.product || e.urls_discovered || e.pages_scraped || 'Pipeline event'}</small></div><time>{String(i + 1).padStart(2, '0')}</time></div>) : <div className="empty-console"><span className="loader-ring"/>Initializing research pipeline…</div>}{job.current_event?.event === 'scrape_started' && <div className="live-line"><span className="live-wave"/> Scraping {job.current_event.region} · {short(job.current_event.url, 72)}</div>}</div>{job.status === 'completed' && <div className="result-banner"><div><span className="result-icon"><Icon name="check" size={16}/></span><div><b>Research complete</b><small>{job.result?.pages_scraped || 0} pages · {job.result?.extractions_ok || 0} structured observations</small></div></div><button className="secondary" onClick={() => setPage('offers')}>Explore evidence <Icon name="arrow" size={14}/></button></div>}{job.status === 'failed' && <div className="error-banner">{job.error}</div>}</> : <Empty title="No active run" text="Your pipeline console will stream discovery, scraping and extraction events here."/>}
      </section>
    </div>
  </div>;
}

function Field({ label, value, onChange, placeholder, type = 'text' }) { return <label className="field"><span>{label}</span><input type={type} value={value} onChange={e => onChange(e.target.value)} placeholder={placeholder} required={label !== 'Focus products'}/></label>; }
function StatusBadge({ status }) { const labels = { idle: 'Ready', queued: 'Queued', running: 'Running', completed: 'Complete', failed: 'Failed' }; return <span className={`status-badge ${status}`}><i/>{labels[status] || status}</span>; }

function ResearchPipeline({ status, events }) {
  const stages = ['Discover', 'Scrape', 'Extract', 'Validate', 'Compare', 'Report'];
  const eventNames = events.map(e => String(e.event || '').toLowerCase());

  const completed = Math.min(
    stages.length,
    events.filter(e => e?.event).length
  );

  const activeIndex = status === 'completed'
    ? stages.length
    : status === 'failed'
      ? Math.min(Math.max(completed - 1, 0), stages.length - 1)
      : Math.min(completed, stages.length - 1);

  return <div className={`research-pipeline ${status || 'idle'}`}>
    {stages.map((stage, i) => {
      const done = status === 'completed' || i < activeIndex;
      const active = status === 'running' && i === activeIndex;
      const hasEvent = eventNames.some(e => e.includes(stage.toLowerCase()));

      return <React.Fragment key={stage}>
        <div className={`research-stage ${done ? 'done' : ''} ${active ? 'active' : ''} ${hasEvent ? 'observed' : ''}`}>
          <span className="stage-dot">{done ? '✓' : String(i + 1).padStart(2, '0')}</span>
          <span>{stage}</span>
        </div>
        {i < stages.length - 1 && <span className={`stage-connector ${done ? 'done' : ''}`} />}
      </React.Fragment>;
    })}
  </div>;
}

function Offers({ runId }) {
  const [data, setData] = useState([]), [region, setRegion] = useState(''), [product, setProduct] = useState(''), [loading, setLoading] = useState(false), [selected, setSelected] = useState(null);
  const load = async () => { setLoading(true); try { const q = `/offers?limit=500${runId ? `&run_id=${runId}` : ''}${region ? `&region=${region}` : ''}${product ? `&product=${encodeURIComponent(product)}` : ''}`; setData(await api(q)); } catch {} finally { setLoading(false); } };
  useEffect(() => { load(); }, [runId, region]);
  const competitors = [...new Set(data.map(x => x.competitor).filter(Boolean))];
  return <div className="page-stack"><div className="page-intro"><div><div className="eyebrow accent">EVIDENCE EXPLORER</div><h2>Every observation, traceable.</h2><p>Filter structured offers by product and region. Open the original source for verification.</p></div><div className="result-count"><b>{data.length}</b><span>observations</span></div></div>
    <section className="surface evidence-surface"><div className="toolbar"><div className="search-box"><span>⌕</span><input placeholder="Search product or competitor…" value={product} onChange={e => setProduct(e.target.value)} onKeyDown={e => e.key === 'Enter' && load()}/></div><div className="toolbar-actions"><select value={region} onChange={e => setRegion(e.target.value)}><option value="">All regions</option>{REGIONS.map(r => <option key={r}>{r}</option>)}</select><button className="secondary" onClick={load}><Icon name="refresh" size={15}/> Refresh</button></div></div>
      {competitors.length > 0 && <div className="filter-summary"><span>Active run: <b>{runId || 'all runs'}</b></span><span>{competitors.length} competitors</span></div>}
      <div className="data-table-wrap"><table className="data-table"><thead><tr><th>Product</th><th>Competitor</th><th>Region</th><th>Price</th><th>USD</th><th>Availability</th><th>Confidence</th><th/></tr></thead><tbody>{data.map((o, i) => <tr key={o.offer_key || i} onClick={() => setSelected(o)}><td><div className="product-cell"><span className="product-mark">{String(o.product_name || '?').slice(0,1)}</span><div><b>{short(o.product_name, 32)}</b><small>{o.brand || o.listing_title || 'Product observation'}</small></div></div></td><td>{o.competitor || '—'}</td><td><span className="tag">{o.region}</span></td><td><b>{money(o.price, o.currency)}</b></td><td>{o.normalized_price_usd != null ? <b>${Number(o.normalized_price_usd).toFixed(2)}</b> : '—'}</td><td><span className="availability"><i/>{pretty(o.availability)}</span></td><td><Confidence value={o.extraction_confidence}/></td><td><button className="row-open" onClick={(e) => { e.stopPropagation(); setSelected(o); }}><Icon name="arrow" size={15}/></button></td></tr>)}{!data.length && <tr><td colSpan="8">{loading ? <div className="loading-row"><span className="loader-ring"/>Loading evidence…</div> : <Empty title="No observations found" text="Run research or adjust your filters." action="Start research" onAction={() => window.scrollTo(0,0)}/>}</td></tr>}</tbody></table></div>
    </section>{selected && <EvidenceDrawer offer={selected} onClose={() => setSelected(null)}/>}</div>;
}
function Confidence({ value }) { if (value == null) return <span className="confidence muted">—</span>; const n = Number(value); return <span className="confidence"><i style={{width: `${Math.max(5, n * 100)}%`}}/><b>{(n * 100).toFixed(0)}%</b></span>; }
function EvidenceDrawer({ offer, onClose }) { return <div className="drawer-backdrop" onClick={onClose}><aside className="drawer" onClick={e => e.stopPropagation()}><div className="drawer-head"><div><div className="eyebrow">OBSERVATION</div><h3>{offer.product_name}</h3></div><button className="icon-button" onClick={onClose}>×</button></div><div className="drawer-price"><span>{money(offer.price, offer.currency)}</span><small>{offer.region} · {offer.competitor || 'Unknown competitor'}</small></div><div className="detail-grid"><Detail label="Availability" value={pretty(offer.availability)}/><Detail label="Confidence" value={offer.extraction_confidence != null ? `${(offer.extraction_confidence*100).toFixed(0)}%` : '—'}/><Detail label="Captured" value={offer.scraped_at ? new Date(offer.scraped_at).toLocaleString() : '—'}/><Detail label="Seller" value={offer.seller || '—'}/></div><div className="evidence-box"><div className="eyebrow">SOURCE</div><p>{offer.url}</p><a className="primary wide" href={offer.url} target="_blank" rel="noreferrer">Open original source <Icon name="external" size={14}/></a></div><div className="drawer-note"><span className="mini-orb"/><div><b>Evidence-first extraction</b><p>The source URL, capture time and structured fields stay attached to this observation for verification.</p></div></div></aside></div>; }
function Detail({ label, value }) { return <div><span>{label}</span><b>{value}</b></div>; }

function Insights({ runId }) {
  const [undercut, setUndercut] = useState([]), [moves, setMoves] = useState([]), [regional, setRegional] = useState([]), [events, setEvents] = useState([]), [changes, setChanges] = useState([]);
  useEffect(() => { Promise.all([api(`/insights/undercut${runId ? `?run_id=${runId}` : ''}`), api(`/insights/price-moves?days_back=30${runId ? `&run_id=${runId}` : ''}`), api(`/insights/regional${runId ? `?run_id=${runId}` : ''}`), api(`/insights/events${runId ? `?run_id=${runId}` : ''}`), api('/insights/changes?limit=20')]).then(([u,m,r,e,c]) => { setUndercut(u); setMoves(m); setRegional(r); setEvents(e); setChanges(c); }).catch(() => {}); }, [runId]);
  const maxGap = Math.max(1, ...undercut.map(x => Math.abs(Number(x.gap_pct || 0))));
  return <div className="page-stack"><div className="page-intro"><div><div className="eyebrow accent">SIGNAL CENTER</div><h2>See what moved.</h2><p>Price pressure, changes and regional differences distilled from the selected research run.</p></div><div className="signal-counter"><span className="pulse-dot"/><b>{events.length}</b> active signals</div></div>
    <div className="insight-grid"><section className="surface"><SectionHead eyebrow="PRICE PRESSURE" title="Undercut map"/>{undercut.length ? <div className="bar-list">{undercut.slice(0, 7).map((x, i) => <div className="bar-item" key={i}><div className="bar-meta"><span><b>{short(x.product_name, 30)}</b><small>{x.leader} · {x.region}</small></span><strong>{x.gap_pct != null ? `${Number(x.gap_pct).toFixed(1)}%` : '—'}</strong></div><div className="bar-track"><i style={{width: `${Math.min(100, Math.abs(Number(x.gap_pct || 0)) / maxGap * 100)}%`}}/></div></div>)}</div> : <Empty title="No price pressure detected" text="Undercut comparisons will appear when the run contains comparable offers."/>}</section>
      <section className="surface"><SectionHead eyebrow="CHANGE DETECTION" title="Recent moves"/>{moves.length ? <div className="move-list">{moves.slice(0, 8).map((x, i) => <div className="move-item" key={i}><span className={`move-icon ${x.event === 'drop' ? 'down' : 'up'}`}>{x.event === 'drop' ? '↓' : '↑'}</span><div><b>{short(x.product_name, 30)}</b><small>{x.region} · {x.from_date} → {x.to_date}</small></div><strong className={x.event === 'drop' ? 'down-text' : 'up-text'}>{Number(x.change_pct).toFixed(1)}%</strong></div>)}</div> : <Empty title="No historical moves yet" text="Run the same competitor set again to unlock time-series change detection."/>}</section>
    </div>
    <section className="surface"><SectionHead eyebrow="CROSS-RUN INTELLIGENCE" title="What changed since the last scan"/><div className="change-grid">{changes.length ? changes.slice(0, 8).map((c, i) => <div className="change-card" key={i}><div className={`change-kind ${c.kind === 'price_drop' ? 'drop' : c.kind === 'price_increase' ? 'increase' : 'new'}`}>{c.kind === 'price_drop' ? '↓' : c.kind === 'price_increase' ? '↑' : '＋'}</div><div className="change-main"><b>{short(c.product_name, 38)}</b><small>{c.competitor || 'Competitor'} · {c.region}</small><p>{c.message}</p></div>{c.change_pct != null && <strong className={c.change_pct < 0 ? 'down-text' : 'up-text'}>{c.change_pct > 0 ? '+' : ''}{Number(c.change_pct).toFixed(1)}%</strong>}</div>) : <Empty title="One scan isn't a trend" text="Run the same competitor set again to compare the latest two research snapshots."/>}</div></section>
    <section className="surface"><SectionHead eyebrow="LIVE SIGNALS" title="Competitive events"/><div className="event-grid">{events.length ? events.slice(0, 12).map((e, i) => <Signal key={i} event={e}/>) : <Empty title="Quiet market" text="No event-level changes were detected for this run."/>}</div></section>
    <section className="surface regional-surface"><SectionHead eyebrow="REGIONAL INTELLIGENCE" title="Market snapshot"/><pre className="json-view">{JSON.stringify(regional, null, 2)}</pre></section>
  </div>;
}

function Ask({ runId }) {
  const [q, setQ] = useState('What are customers complaining about most?'), [answer, setAnswer] = useState(null), [busy, setBusy] = useState(false);
  const ask = async e => { e?.preventDefault(); if (!q.trim()) return; setBusy(true); try { setAnswer(await api('/insights/reviews/query', { method: 'POST', body: JSON.stringify({ question: q, run_id: runId || null, n: 8 }) })); } catch (err) { setAnswer({ answer: err.message, sources: [] }); } finally { setBusy(false); } };
  return <div className="page-stack"><div className="page-intro"><div><div className="eyebrow accent">EVIDENCE-BACKED RAG</div><h2>Ask the corpus.</h2><p>Ask natural-language questions over the retrieved review evidence. Answers expose their source material.</p></div></div>
    <section className="surface ask-surface"><div className="ask-glow"/><div className="ask-hero"><div className="ask-orb"><Icon name="spark" size={28}/></div><div><span>AI research assistant</span><h3>What do customers really say?</h3></div></div><form onSubmit={ask} className="ask-form"><textarea value={q} onChange={e => setQ(e.target.value)} rows="4" placeholder="Ask about complaints, praise, features, price perception…"/><div className="ask-footer"><span><span className="status-dot live"/> Scoped to {runId ? `run ${runId}` : 'all available evidence'}</span><button className="primary" disabled={busy}>{busy ? <><span className="loader-dot"/> Searching evidence…</> : <>Ask question <Icon name="arrow" size={15}/></>}</button></div></form>{answer && <div className="answer-panel"><div className="answer-head"><span className="answer-label">SYNTHESIS</span><span className="answer-model">Retrieved evidence · {answer.sources?.length || 0} sources</span></div><p>{answer.answer}</p><div className="sources">{(answer.sources || []).map((s, i) => <a key={i} href={s.source_url} target="_blank" rel="noreferrer"><span>[{i + 1}]</span>{short(s.review_text || s.source_url, 110)}<Icon name="external" size={13}/></a>)}</div></div>}</section>
    <div className="prompt-chips"><button onClick={() => setQ('Which features receive the most praise?')}>Most praised features</button><button onClick={() => setQ('What are the recurring complaints?')}>Recurring complaints</button><button onClick={() => setQ('How is price perception changing?')}>Price perception</button></div>
  </div>;
}

function Reports() {
  const [reports, setReports] = useState([]); useEffect(() => { api('/reports').then(setReports).catch(() => {}); }, []);
  return <div className="page-stack"><div className="page-intro"><div><div className="eyebrow accent">REPORTING</div><h2>Decision-ready outputs.</h2><p>Generated reports stay linked to their research run so the underlying evidence remains auditable.</p></div></div><section className="surface"><SectionHead eyebrow="PDF REPORTS" title="Report library"/><div className="report-list">{reports.length ? reports.map(r => <div className="report-item" key={r}><div className="pdf-mark">PDF</div><div><b>{r.replace(/^report_|\.pdf$/g, '')}</b><small>Generated competitive intelligence report</small></div><a className="secondary" href={`${API}/reports/${r.replace(/^report_|\.pdf$/g,'')}`} target="_blank" rel="noreferrer">Open report <Icon name="external" size={14}/></a></div>) : <Empty title="No reports yet" text="Generate a PDF after a completed research run with scripts/generate_report.py."/>}</div></section></div>;
}

function Empty({ title = 'Nothing here yet', text = '', action, onAction }) { return <div className="empty-state"><div className="empty-icon"><Icon name="spark" size={18}/></div><div><b>{title}</b><p>{text}</p>{action && <button className="link-btn" onClick={onAction}>{action}<Icon name="arrow" size={14}/></button>}</div></div>; }

// ------------------------------------------------------------------
// deep research
// ------------------------------------------------------------------
const DEEP_MODES = [['url', 'URL', 'globe'], ['topic', 'Topic', 'spark'], ['compare', 'Compare', 'layers']];
const DEEP_TABS = [['report', 'Report'], ['findings', 'Findings'], ['gaps', 'Gaps'], ['pages', 'Pages'], ['sources', 'Sources']];

function Markdown({ text }) {
  const blocks = useMemo(() => parseMarkdown(text || ''), [text]);
  return <div className="markdown">{blocks.map((block, i) => {
    if (block.type === 'heading') {
      const Tag = block.level === 1 ? 'h2' : block.level === 2 ? 'h3' : 'h4';
      return <Tag key={i}>{inline(block.text)}</Tag>;
    }
    if (block.type === 'list') return <ul key={i}>{block.items.map((item, j) => <li key={j}>{inline(item)}</li>)}</ul>;
    if (block.type === 'quote') return <blockquote key={i}>{inline(block.text)}</blockquote>;
    return <p key={i}>{inline(block.text)}</p>;
  })}</div>;
}

// Renders the subset of Markdown the research report actually emits.
function parseMarkdown(text) {
  const out = [];
  let list = null;
  const flush = () => { if (list) { out.push(list); list = null; } };
  for (const raw of String(text).split('\n')) {
    const line = raw.trim();
    if (!line) { flush(); continue; }
    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) { flush(); out.push({ type: 'heading', level: heading[1].length, text: heading[2] }); continue; }
    const bullet = line.match(/^[-*+]\s+(.*)$/);
    if (bullet) { if (!list) list = { type: 'list', items: [] }; list.items.push(bullet[1]); continue; }
    if (/^>\s?/.test(line)) { flush(); out.push({ type: 'quote', text: line.replace(/^>\s?/, '') }); continue; }
    flush();
    out.push({ type: 'para', text: line });
  }
  flush();
  return out;
}

// Turns **bold**, `code` and [S1] citation markers into elements.
function inline(text) {
  const parts = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`|\[(S\d+)\]|\[[^\]]+\]\((https?:\/\/[^)]+)\))/g;
  let last = 0;
  let match;
  while ((match = re.exec(text))) {
    if (match.index > last) parts.push(text.slice(last, match.index));
    const token = match[0];
    if (token.startsWith('**')) parts.push(<b key={match.index}>{token.slice(2, -2)}</b>);
    else if (token.startsWith('`')) parts.push(<code key={match.index}>{token.slice(1, -1)}</code>);
    else if (match[3]) parts.push(<a key={match.index} href={match[3]} target="_blank" rel="noreferrer">{token.slice(1, token.indexOf(']('))}</a>);
    else parts.push(<span className="cite-chip" key={match.index} title="Source reference">{match[2]}</span>);
    last = match.index + token.length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

function DeepResearch({ deepRun, setDeepRun, onChat }) {
  const [mode, setMode] = useState('url');
  const [url, setUrl] = useState('');
  const [topic, setTopic] = useState('');
  const [rivals, setRivals] = useState('');
  const [question, setQuestion] = useState('');
  const [pages, setPages] = useState(8);
  const [useSearch, setUseSearch] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [tab, setTab] = useState('report');

  const run = async (e) => {
    e?.preventDefault();
    setBusy(true); setError('');
    try {
      const payload = {
        mode,
        url: mode === 'url' ? url.trim() : null,
        topic: mode === 'topic' ? topic.trim() : null,
        competitors: mode === 'compare' ? rivals.split(',').map(s => s.trim()).filter(Boolean) : [],
        question: question.trim(),
        max_pages: pages,
        use_search: useSearch,
      };
      const result = await api('/research/deep', { method: 'POST', body: JSON.stringify(payload) });
      setDeepRun(result);
      setTab('report');
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const coverage = deepRun?.coverage;
  const citations = deepRun?.citations || [];

  return <div className="page-stack">
    <div className="page-intro">
      <div>
        <div className="eyebrow accent">DEEP RESEARCH</div>
        <h2>Crawl a site. Read it properly.</h2>
        <p>Budgeted crawling, heading-aware extraction and cited reporting. Every line of the report points back to the page it came from.</p>
      </div>
    </div>

    <section className="surface deep-form">
      <div className="mode-tabs">{DEEP_MODES.map(([id, label, icon]) => <button key={id} className={mode === id ? 'active' : ''} onClick={() => setMode(id)}><Icon name={icon} size={16}/>{label}</button>)}</div>
      <form onSubmit={run}>
        {mode === 'url' && <Field label="Site URL" value={url} onChange={setUrl} placeholder="https://acme.com" type="url"/>}
        {mode === 'topic' && <Field label="Topic" value={topic} onChange={setTopic} placeholder="AI note-taking apps pricing"/>}
        {mode === 'compare' && <><Field label="Subject" value={topic} onChange={setTopic} placeholder="Notion"/><Field label="Competitors (comma separated)" value={rivals} onChange={setRivals} placeholder="Coda, Obsidian, Anytype"/></>}
        <label className="field"><span>What should the research answer?</span><textarea rows="3" value={question} onChange={e => setQuestion(e.target.value)} placeholder="What does it charge, what limits apply, and what are the trial terms?"/></label>
        <div className="deep-options">
          <label className="slider-field"><span>Page budget <b>{pages}</b></span><input type="range" min="2" max="30" value={pages} onChange={e => setPages(Number(e.target.value))}/></label>
          <label className="toggle-field"><input type="checkbox" checked={useSearch} onChange={e => setUseSearch(e.target.checked)}/><span>Web search for extra sources</span></label>
        </div>
        {error && <div className="inline-error">{error}</div>}
        <div className="deep-actions">
          <button className="primary" disabled={busy}>{busy ? <><span className="loader-dot"/> Researching…</> : <><Icon name="compass" size={16}/> Start deep research</>}</button>
          <span className="hint">Runs synchronously. Larger budgets take longer.</span>
        </div>
      </form>
    </section>

    {deepRun && <>
      <div className="deep-stats">
        <Metric icon="layers" label="Pages" value={coverage?.pages_fetched ?? 0} hint={`${coverage?.pages_failed || 0} failed`}/>
        <Metric icon="file" label="Sections" value={coverage?.sections ?? 0} hint={`${coverage?.facts || 0} facts`}/>
        <Metric icon="spark" label="Status" value={pretty(deepRun.status)} hint={`${coverage?.characters || 0} chars`}/>
        <Metric icon="clock" label="Duration" value={`${deepRun.duration_s ?? 0}s`} hint={coverage?.search_backend ? `search: ${coverage.search_backend}` : 'no search'}/>
      </div>

      <section className="surface deep-result">
        <div className="result-head">
          <div className="tab-row">{DEEP_TABS.map(([id, label]) => <button key={id} className={tab === id ? 'active' : ''} onClick={() => setTab(id)}>{label}{id === 'gaps' && deepRun.gaps?.length ? <em>{deepRun.gaps.length}</em> : null}{id === 'sources' && citations.length ? <em>{citations.length}</em> : null}</button>)}</div>
          <button className="secondary" onClick={onChat}><Icon name="message" size={15}/> Ask this run</button>
        </div>

        {tab === 'report' && (deepRun.report_markdown
          ? <Markdown text={deepRun.report_markdown}/>
          : <Empty title="No report produced" text="The run finished without usable evidence."/>)}

        {tab === 'findings' && (deepRun.findings?.length
          ? <ul className="finding-list">{deepRun.findings.map((f, i) => <li key={i}>{inline(String(f))}</li>)}</ul>
          : <Empty title="No findings" text="No evidence-backed statements were captured."/>)}

        {tab === 'gaps' && (deepRun.gaps?.length
          ? <ul className="gap-list">{deepRun.gaps.map((g, i) => <li key={i}><b>{g.question}</b><span>{g.reason}</span></li>)}</ul>
          : <Empty title="No open questions" text="Every planned question found supporting evidence."/>)}

        {tab === 'pages' && (deepRun.pages?.length
          ? <div className="page-table">{deepRun.pages.map(p => <div className="page-row" key={p.url}><span className="cite-chip">{p.source_ref}</span><a href={p.url} target="_blank" rel="noreferrer">{short(p.url, 58)}<Icon name="external" size={13}/></a><em>{pretty(p.page_type)}</em><i className={`pill ${p.status}`}>{p.status}</i><small>{(p.chars || 0).toLocaleString()} chars</small></div>)}</div>
          : <Empty title="No pages visited" text="The crawl frontier stayed empty."/>)}

        {tab === 'sources' && (citations.length
          ? <div className="source-list">{citations.map(c => <a key={c.ref} href={c.url} target="_blank" rel="noreferrer"><span className="cite-chip">{c.ref}</span><div><b>{c.title || c.url}</b><small>{c.heading || pretty(c.url)}</small></div><Icon name="external" size={14}/></a>)}</div>
          : <Empty title="No sources" text="Nothing citable was captured."/>)}
      </section>
    </>}
  </div>;
}

// ------------------------------------------------------------------
// research chat
// ------------------------------------------------------------------
const CHAT_STARTERS = [
  'What does it charge per month?',
  'Which limits and guarantees are stated?',
  'What did customers complain about?',
  'What is still unclear from the evidence?',
];

function ResearchChat({ runId, runTitle }) {
  const [runs, setRuns] = useState([]);
  const [activeRun, setActiveRun] = useState(runId || '');
  const [sessionId, setSessionId] = useState(() => localStorage.getItem('acr_chat_session') || '');
  const [messages, setMessages] = useState([]);
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => { api('/research/deep/runs').then(setRuns).catch(() => {}); }, []);
  useEffect(() => { if (runId) setActiveRun(runId); }, [runId]);

  const loadSession = async (id) => {
    if (!id) { setMessages([]); return; }
    try {
      const data = await api(`/research/chat/${id}`);
      setMessages((data.messages || []).map(m => ({ role: m.role, content: m.content, citations: safeJson(m.citations) })));
    } catch { setMessages([]); }
  };

  useEffect(() => { loadSession(sessionId); }, [sessionId]);

  const send = async (e) => {
    e?.preventDefault();
    const question = text.trim();
    if (!question || busy) return;
    setBusy(true);
    setText('');
    setMessages(prev => [...prev, { role: 'user', content: question, citations: [] }]);
    try {
      const answer = await api('/research/chat', { method: 'POST', body: JSON.stringify({ question, run_id: activeRun || null, session_id: sessionId || null }) });
      if (answer.session_id && answer.session_id !== sessionId) {
        localStorage.setItem('acr_chat_session', answer.session_id);
        setSessionId(answer.session_id);
      }
      setMessages(prev => [...prev, { role: 'assistant', content: answer.message, citations: answer.citations || [], answerable: answer.answerable }]);
    } catch (err) {
      setMessages(prev => [...prev, { role: 'assistant', content: err.message, citations: [], answerable: false }]);
    } finally {
      setBusy(false);
    }
  };

  const startNew = () => {
    localStorage.removeItem('acr_chat_session');
    setSessionId('');
    setMessages([]);
  };

  const selected = runs.find(r => r.run_id === activeRun);

  return <div className="page-stack">
    <div className="page-intro">
      <div>
        <div className="eyebrow accent">RESEARCH CHAT</div>
        <h2>Interrogate the evidence.</h2>
        <p>Answers are assembled only from sections a run actually captured, and each claim keeps its source marker.</p>
      </div>
    </div>

    <section className="surface chat-surface">
      <aside className="chat-rail">
        <div className="chat-rail-head"><span>Scope</span><button className="link-btn" onClick={startNew}>New chat</button></div>
        <select className="run-select" value={activeRun} onChange={e => setActiveRun(e.target.value)}>
          <option value="">Latest research run</option>
          {runs.map(r => <option key={r.run_id} value={r.run_id}>{r.run_id} · {pretty(r.status)}</option>)}
        </select>
        <div className="chat-scope">
          {selected ? <>
            <b>{runTitle || selected.question || selected.plan?.subject || selected.run_id}</b>
            <small>{selected.coverage?.pages_fetched || 0} pages · {selected.coverage?.sections || 0} sections · {selected.coverage?.facts || 0} facts</small>
          </> : <small>{runs.length ? 'Using the most recent run.' : 'Run deep research first to build a corpus.'}</small>}
        </div>
      </aside>

      <div className="chat-main">
        <div className="chat-log">
          {messages.length === 0 && <div className="chat-welcome">
            <div className="ask-orb"><Icon name="message" size={26}/></div>
            <h3>Ask about the captured evidence</h3>
            <p>Questions the evidence cannot answer are reported as gaps rather than guessed.</p>
            <div className="prompt-chips">{CHAT_STARTERS.map(s => <button key={s} onClick={() => setText(s)}>{s}</button>)}</div>
          </div>}
          {messages.map((m, i) => <div key={i} className={`chat-msg ${m.role}`}>
            <div className="chat-bubble">{inline(m.content)}</div>
            {!!m.citations?.length && <div className="chat-cites">{m.citations.map((c, j) => <a key={j} href={c.url} target="_blank" rel="noreferrer"><span className="cite-chip">{c.ref}</span>{short(c.heading || c.title || c.url, 46)}<Icon name="external" size={12}/></a>)}</div>}
            {m.answerable === false && <span className="chat-flag">evidence gap</span>}
          </div>)}
          {busy && <div className="chat-msg assistant"><div className="chat-bubble"><span className="loader-dot"/> Reading the run…</div></div>}
        </div>
        <form className="chat-composer" onSubmit={send}>
          <textarea rows="2" value={text} onChange={e => setText(e.target.value)} placeholder={runs.length ? 'Ask a question about the selected run…' : 'Run deep research first to build a corpus…'} disabled={busy}/>
          <button className="primary" disabled={busy || !text.trim()}><Icon name="arrow" size={15}/></button>
        </form>
      </div>
    </section>
  </div>;
}

function safeJson(value) {
  if (Array.isArray(value)) return value;
  try { return JSON.parse(value || '[]'); } catch { return []; }
}

createRoot(document.getElementById('root')).render(<App />);
