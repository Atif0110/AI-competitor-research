import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './styles.css';

const API = (import.meta.env.VITE_API_URL || 'http://localhost:8000').replace(/\/$/, '');

const REGIONS = ['US', 'UK', 'IN', 'EU', 'JP', 'CA', 'AU', 'SG', 'BR'];

const READ_TIMEOUT_MS = 20000;
const WRITE_TIMEOUT_MS = 120000;
const DEEP_TIMEOUT_MS = 720000;

function requestTimeoutMs(path, method) {
  if (method !== 'GET' && String(path).startsWith('/research/deep')) return DEEP_TIMEOUT_MS;
  if (method !== 'GET') return WRITE_TIMEOUT_MS;
  return READ_TIMEOUT_MS;
}

/* ------------------------------------------------------------------ */
/* data access                                                         */
/* ------------------------------------------------------------------ */
async function api(path, options = {}) {
  const { timeoutMs: timeoutOverride, headers: extraHeaders, ...fetchOptions } = options;
  const headers = {
    ...(fetchOptions.body ? { 'Content-Type': 'application/json' } : {}),
    ...(extraHeaders || {}),
  };

  const method = fetchOptions.method || 'GET';
  // Only idempotent reads are retried, so a research job can never be started twice.
  const attempts = method === 'GET' ? 2 : 1;
  const timeoutMs = timeoutOverride || requestTimeoutMs(path, method);

  let lastError = null;

  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);

    let response;
    try {
      response = await fetch(`${API}${path}`, { ...fetchOptions, headers, signal: controller.signal });
    } catch (error) {
      clearTimeout(timer);
      lastError = error?.name === 'AbortError' ? 'timed out' : 'could not connect';
      if (attempt < attempts) continue;
      throw new Error(
        `The research API at ${API} ${lastError}. ` +
          'If this persists, check that the API service is running (a suspended Render service answers with "Service Suspended"), and that CORS_ORIGINS on the API includes this site origin.',
      );
    }
    clearTimeout(timer);

    const text = await response.text();

    // Render serves an HTML error page for suspended/unavailable services.
    // The browser hides it behind a CORS failure, so read it explicitly.
    if (/^\s*<!doctype html|^\s*<html/i.test(text) && !text.trimStart().startsWith('{')) {
      if (/suspended/i.test(text)) {
        throw new Error(
          'The API service is suspended by its owner. Resume it in the Render dashboard, then reload.',
        );
      }
      throw new Error(
        `The API service returned an HTML error page (${response.status}) instead of data. It is likely restarting or unavailable.`,
      );
    }

    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = text;
    }

    if (response.status === 401) throw new Error('This workspace is not accepting public research requests yet. The service owner needs to enable public workspace access in the backend deployment.');
    if (response.status === 503) {
      throw new Error(
        data?.detail ||
          'The API service refused the request (503). Check the backend deployment’s authentication and provider configuration.',
      );
    }
    if (!response.ok) {
      const detail = data?.detail;
      const message = Array.isArray(detail)
        ? detail.map((d) => d.msg || d.message || JSON.stringify(d)).join('; ')
        : detail || `Request failed (${response.status})`;
      throw new Error(message);
    }
    return data;
  }

  throw new Error(`The research API at ${API} ${lastError || 'is unreachable'}.`);
}

const money = (n, currency = 'USD') =>
  n == null
    ? '—'
    : Number(n) === 0
      ? 'Free'
    : currency === 'USD'
      ? `$${Number(n).toFixed(2)}`
      : `${Number(n).toFixed(2)} ${currency}`;

const short = (s, n = 64) => (s?.length > n ? `${s.slice(0, n)}…` : s || '—');
const pretty = (s) => String(s || '').replaceAll('_', ' ');
const num = (n) => (n == null ? '—' : Number(n).toLocaleString());

function safeJson(value) {
  if (Array.isArray(value)) return value;
  try {
    return JSON.parse(value || '[]');
  } catch {
    return [];
  }
}

/* ------------------------------------------------------------------ */
/* icons                                                               */
/* ------------------------------------------------------------------ */
const ICONS = {
  grid: <><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></>,
  target: <><circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3.5"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/></>,
  compass: <><circle cx="12" cy="12" r="8.5"/><path d="m15.4 8.6-2 4.8-4.8 2 2-4.8z"/></>,
  layers: <><path d="m12 3 8 4-8 4-8-4z"/><path d="m4 12 8 4 8-4"/><path d="m4 17 8 4 8-4"/></>,
  pulse: <path d="M3 12h4l3-7 4 14 3-7h4"/>,
  chat: <><path d="M20 11.5a7.5 7.5 0 0 1-10.9 6.7L4 20l1.4-4.2A7.5 7.5 0 1 1 20 11.5Z"/></>,
  doc: <><path d="M6 2h8l4 4v16H6z"/><path d="M14 2v5h5"/><path d="M9 13h6M9 17h4"/></>,
  arrow: <><path d="M5 12h13"/><path d="m12 5 6 7-6 7"/></>,
  plus: <><path d="M12 5v14M5 12h14"/></>,
  external: <><path d="M14 4h6v6"/><path d="m20 4-9 9"/><path d="M18 13v6a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h6"/></>,
  check: <path d="m5 12 4.5 4.5L19 7"/>,
  alert: <><path d="M12 8v5"/><path d="M12 17h.01"/><circle cx="12" cy="12" r="9"/></>,
  menu: <><path d="M4 7h16M4 12h16M4 17h16"/></>,
  refresh: <><path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/></>,
  key: <><circle cx="8" cy="12" r="4"/><path d="M12 12h9"/><path d="M17 12v3"/></>,
  search: <><circle cx="11" cy="11" r="6"/><path d="m20 20-4.5-4.5"/></>,
};

function Icon({ name, size = 18 }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {ICONS[name] || ICONS.grid}
    </svg>
  );
}

/* ------------------------------------------------------------------ */
/* small shared pieces                                                 */
/* ------------------------------------------------------------------ */
function Stat({ label, value, unit, note, tone = 'default' }) {
  return (
    <div className={`stat stat-${tone}`}>
      <span className="stat-label">{label}</span>
      <strong className="stat-value">
        {value}
        {unit && <em>{unit}</em>}
      </strong>
      {note && <small className="stat-note">{note}</small>}
    </div>
  );
}

function Masthead({ eyebrow, title, lede, actions }) {
  return (
    <header className="masthead">
      <div className="masthead-text">
        <span className="eyebrow">{eyebrow}</span>
        <h1>{title}</h1>
        {lede && <p>{lede}</p>}
      </div>
      {actions && <div className="masthead-actions">{actions}</div>}
    </header>
  );
}

function Empty({ title, text, action, onAction }) {
  return (
    <div className="empty">
      <strong>{title}</strong>
      <p>{text}</p>
      {action && (
        <button className="btn-ghost" onClick={onAction}>
          {action}
        </button>
      )}
    </div>
  );
}

function Badge({ children, tone = 'neutral' }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

function citeTone(ref) {
  return String(ref || '').toLowerCase();
}

/* ------------------------------------------------------------------ */
/* app shell                                                           */
/* ------------------------------------------------------------------ */
function App() {
  const [page, setPage] = useState('dashboard');
  const [health, setHealth] = useState(null);
  const [toast, setToast] = useState('');
  const [runId, setRunId] = useState(null);
  const [chatRunId, setChatRunId] = useState(null);
  const [chatSessionId, setChatSessionId] = useState(null);
  const [deepRun, setDeepRun] = useState(null);
  const [navOpen, setNavOpen] = useState(false);
  const [navFilter, setNavFilter] = useState('');
  const navSearchRef = useRef(null);

  const loadHealth = () =>
    api('/health', { timeoutMs: 12000 })
      .then(setHealth)
      .catch((error) => {
        setHealth({ status: 'error', message: error.message });
        console.warn('Research API health check failed:', error.message);
      });

  useEffect(() => {
    loadHealth();
  }, []);

  useEffect(() => {
    const handleShortcut = (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        navSearchRef.current?.focus();
      }
      if (event.key === 'Escape') {
        setNavOpen(false);
        setNavFilter('');
        navSearchRef.current?.blur();
      }
    };
    window.addEventListener('keydown', handleShortcut);
    return () => window.removeEventListener('keydown', handleShortcut);
  }, []);

  useEffect(() => {
    if (!toast) return undefined;
    const timer = setTimeout(() => setToast(''), 6000);
    return () => clearTimeout(timer);
  }, [toast]);

  const startRun = async (target) => {
    try {
      const result = await api('/research/jobs', { method: 'POST', body: JSON.stringify(target) });
      setRunId(result.job_id);
      setPage('run');
      setNavOpen(false);
    } catch (error) {
      setToast(error.message);
    }
  };

  const openSavedRun = async (run) => {
    if (run.kind === 'deep') {
      try {
        const saved = await api(`/research/deep/runs/${run.run_id}`);
        setDeepRun(saved);
        setPage('deep');
      } catch (error) {
        setToast(`Could not load saved run: ${error.message}`);
      }
      return;
    }
    setRunId(run.run_id);
    setPage('run');
  };

  const askAboutRun = (id) => {
    setChatRunId(id);
    setChatSessionId(null);
    setPage('chat');
  };

  const openSavedChat = (chat) => {
    setChatRunId(chat.run_id || null);
    setChatSessionId(chat.session_id);
    setPage('chat');
  };

  const clearCurrentRun = () => {
    setRunId(null);
  };

  const online = health?.status === 'ok';
  const navGroups = [
    { label: 'Workspace', items: [['dashboard', 'Overview', 'grid'], ['history', 'Research history', 'layers']] },
    { label: 'Build the research', items: [['run', 'Competitive run', 'target'], ['deep', 'Deep research', 'compass']] },
    { label: 'Review findings', items: [['evidence', 'Evidence library', 'doc'], ['signals', 'Market signals', 'pulse'], ['chat', 'Research chat', 'chat'], ['reports', 'Reports', 'doc']] },
  ];
  const normalizedNavFilter = navFilter.trim().toLowerCase();
  const pageTitle = Object.fromEntries(navGroups.flatMap((group) => group.items.map(([id, label]) => [id, label])))[page] || 'Research workspace';
  const navigate = (nextPage) => {
    setPage(nextPage);
    setNavOpen(false);
    setNavFilter('');
  };

  return (
    <div className={`workspace-shell${navOpen ? ' workspace-nav-open' : ''}`}>
      <a className="skip" href="#main">
        Skip to content
      </a>

      <aside className="workspace-sidebar" aria-label="Workspace navigation">
        <button className="site-brand" onClick={() => navigate('dashboard')} aria-label="Scope intelligence home">
          <span className="site-brand-mark"><i /><i /><i /></span>
          <span className="site-brand-copy"><strong>Scope</strong><small>MARKET INTELLIGENCE</small></span>
        </button>
        <label className="workspace-search">
          <Icon name="search" size={16} />
          <input ref={navSearchRef} value={navFilter} onChange={(event) => setNavFilter(event.target.value)} placeholder="Go to…" aria-label="Find a workspace section" />
          <kbd>⌘K</kbd>
        </label>
        <nav className="workspace-nav" aria-label="Main navigation">
          {navGroups.map((group) => {
            const items = group.items.filter(([, label]) => !normalizedNavFilter || label.toLowerCase().includes(normalizedNavFilter));
            if (!items.length) return null;
            return <section className="workspace-nav-group" key={group.label}>
              <h2>{group.label}</h2>
              {items.map(([id, label, icon]) => (
                <button key={id} className={`workspace-nav-item${page === id ? ' is-active' : ''}`} onClick={() => navigate(id)} aria-current={page === id ? 'page' : undefined}>
                  <Icon name={icon} size={16} /><span>{label}</span>
                </button>
              ))}
            </section>;
          })}
          {normalizedNavFilter && !navGroups.some((group) => group.items.some(([, label]) => label.toLowerCase().includes(normalizedNavFilter))) && <p className="workspace-search-empty">No matching sections</p>}
        </nav>
        <div className="workspace-sidebar-footer">
          <div className={`workspace-service${online ? ' is-online' : health?.status === 'error' ? ' is-offline' : ''}`} aria-live="polite">
            <span className="connection-dot" /><span><strong>{online ? 'Service ready' : health?.status === 'error' ? 'Service offline' : 'Connecting'}</strong><small>Research API</small></span>
          </div>
          <span className="workspace-local-note">Evidence saved to your workspace</span>
        </div>
      </aside>
      {navOpen && <button className="workspace-nav-scrim" onClick={() => setNavOpen(false)} aria-label="Close navigation" />}

      <main className="main" id="main">
        <header className="workspace-topbar">
          <button className="workspace-mobile-toggle" onClick={() => setNavOpen((open) => !open)} aria-label={navOpen ? 'Close navigation' : 'Open navigation'} aria-expanded={navOpen}><Icon name="menu" /></button>
          <div className="workspace-crumb"><span>Scope</span><span aria-hidden="true">/</span><strong>{pageTitle}</strong></div>
          <div className="workspace-topbar-actions">
            <div className={`connection-pill${online ? ' is-online' : health?.status === 'error' ? ' is-offline' : ''}`} aria-live="polite" title={health?.llm_provider_chain?.length ? `Route: ${health.llm_provider_chain.join(' / ')}` : undefined}><span className="connection-dot" /><span>{online ? 'Connected' : health?.status === 'error' ? 'Offline' : 'Connecting'}</span></div>
            <button className="btn-primary site-new" onClick={() => navigate('run')} aria-label="New research"><Icon name="plus" size={15} /><span>New research</span></button>
          </div>
        </header>
        <div className="content">
          {health?.status === 'error' && (
            <div className="banner" role="alert">
              <Icon name="alert" size={16} />
              <div>
                <strong>Research service is offline</strong>
                <p>Check the API service status, then retry your connection.</p>
              </div>
              <button className="btn-ghost" onClick={loadHealth}>
                Retry
              </button>
            </div>
          )}

          {page === 'dashboard' && <Dashboard runId={runId} onNavigate={setPage} onOpenRun={openSavedRun} health={health} />}
          {page === 'history' && <ResearchHistory onOpenRun={openSavedRun} onOpenChat={openSavedChat} notify={setToast} />}
          {page === 'run' && <CompetitiveRun onStart={startRun} runId={runId} onClearRun={clearCurrentRun} onViewEvidence={() => setPage('evidence')} onAsk={askAboutRun} notify={setToast} />}
          {page === 'deep' && (
            <DeepResearch
              result={deepRun}
              onResult={setDeepRun}
              onChat={() => askAboutRun(deepRun?.run_id)}
              notify={setToast}
              health={health}
              onNeedKey={() => setToast('This workspace is not accepting public research requests yet. The service owner needs to enable public workspace access in the backend deployment.')}
            />
          )}
          {page === 'evidence' && <Evidence runId={runId} notify={setToast} />}
          {page === 'signals' && <Signals runId={runId} />}
          {page === 'chat' && <ResearchChat runId={chatRunId || deepRun?.run_id || null} sessionId={chatSessionId} notify={setToast} />}
          {page === 'reports' && <Reports />}
        </div>
      </main>

      {toast && (
        <div className="toast" role="status">
          <Icon name="alert" size={16} />
          <span>{toast}</span>
          <button onClick={() => setToast('')} aria-label="Dismiss">
            ×
          </button>
        </div>
      )}

    </div>
  );
}

/* ------------------------------------------------------------------ */
/* overview                                                            */
/* ------------------------------------------------------------------ */
function Dashboard({ runId, onNavigate, onOpenRun, health }) {
  const [metrics, setMetrics] = useState(null);
  const [history, setHistory] = useState([]);
  const [historyError, setHistoryError] = useState('');
  const [loading, setLoading] = useState(true);
  const [metricsError, setMetricsError] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    let current = true;
    setLoading(true);
    setMetricsError(false);
    setHistoryError('');
    Promise.allSettled([
      api('/metrics/aggregate'),
      api('/research/runs?limit=100'),
    ]).then(([aggregateResult, historyResult]) => {
      if (!current) return;
      if (aggregateResult.status === 'fulfilled' && aggregateResult.value) {
        setMetrics(aggregateResult.value);
      } else {
        setMetrics(null);
        setMetricsError(true);
      }
      if (historyResult.status === 'fulfilled' && Array.isArray(historyResult.value)) {
        setHistory(historyResult.value);
      } else {
        setHistory([]);
        setHistoryError(historyResult.reason?.message || 'The API did not return saved runs.');
      }
    }).finally(() => {
      if (current) setLoading(false);
    });
    return () => { current = false; };
  }, [runId, refreshKey]);

  const offers = health?.offers;
  const products = metrics?.total_products;
  const regions = metrics?.total_regions;
  const latestRun = history[0];
  const latestStats = latestRun?.kind === 'deep' ? latestRun.coverage : latestRun?.metrics;
  const latestSources = latestRun?.kind === 'deep' ? latestStats?.pages_fetched : latestStats?.urls_succeeded;
  const latestOffers = latestRun?.kind === 'competitive' ? latestRun.offers_captured : null;

  return (
    <>
      <section className="landing-hero">
        <div className="landing-copy">
          <div className="landing-eyebrow"><span /> COMPETITOR RESEARCH · SOURCE LED</div>
          <h1>Read the market<br />between the <em>moves.</em></h1>
          <p>Track competitor offers and market signals from the original page to the final finding. Every claim stays connected to its source.</p>
          <div className="landing-actions">
            <button className="btn-primary" onClick={() => onNavigate('run')}>Start a comparison <Icon name="arrow" size={15} /></button>
            <button className="editorial-link" onClick={() => onNavigate('deep')}>Explore a market question <Icon name="arrow" size={14} /></button>
          </div>
          <div className="landing-caption"><span className={`connection-dot${health?.status === 'ok' ? ' connection-dot-live' : ''}`} /> {health?.status === 'ok' ? `Research system online · ${health.active_llm_provider || 'AI route ready'}` : 'Connecting to research system'}</div>
        </div>

        <div className="landing-preview" aria-label="Preview of the research workspace">
          <div className="preview-window">
            <div className="preview-chrome">
              <span className="preview-lights"><i /><i /><i /></span>
              <span className="preview-address"><span className="site-brand-mark"><i /><i /><i /></span> scope / research desk</span>
              <span className="preview-chrome-status"><i /> {latestRun ? pretty(latestRun.status) : 'READY'}</span>
            </div>
            <div className="preview-body">
              <aside className="preview-index">
                <span>RESEARCH DESK</span>
                <strong>{latestRun ? latestRun.kind === 'deep' ? 'Deep research' : 'Competitive study' : 'New study'}</strong>
                <div className="preview-index-rule" />
                <small>THE EVIDENCE PATH</small>
                <div className={`preview-step${latestRun && latestSources ? ' is-done' : ''}`}><i /><span>Source pages</span></div>
                <div className={`preview-step${latestOffers ? ' is-done' : ''}`}><i /><span>Structured offers</span></div>
                <div className="preview-step"><i /><span>Market signals</span></div>
              </aside>
              <div className="preview-report">
                <div className="preview-report-head">
                  <div><small>{latestRun ? 'MOST RECENT STUDY' : 'YOUR PRIVATE RESEARCH WORKSPACE'}</small><h2>{latestRun?.title || 'A clearer view of your competitors.'}</h2></div>
                  {latestRun && <button className="preview-open" onClick={() => onOpenRun(latestRun)} aria-label="Open latest saved research"><Icon name="arrow" size={17} /></button>}
                </div>
                <div className="preview-coverage">
                  <div className="coverage-heading"><span>Evidence coverage</span><small>{latestRun ? `${num(latestSources)} pages captured` : 'Your captured evidence, at a glance'}</small></div>
                  <div className="coverage-metrics">
                    <div><strong>{latestRun && latestSources != null ? num(latestSources) : '—'}</strong><small>Source pages captured</small></div>
                    <div><strong>{latestRun && latestOffers != null ? num(latestOffers) : '—'}</strong><small>Validated offers</small></div>
                  </div>
                </div>
                <div className="preview-foot">
                  <div><small>RESEARCH PATH</small><strong>{latestRun ? latestRun.kind === 'deep' ? 'Deep research' : 'Competitive intelligence' : 'Company · category · region'}</strong></div>
                  <div><small>COMPUTE</small><strong>{latestRun?.duration_s ? `${num(latestRun.duration_s)} sec` : 'Usage tracked per run'}</strong></div>
                  <div><small>SOURCE TRAIL</small><strong>Open evidence at the claim</strong></div>
                </div>
              </div>
            </div>
          </div>
          <div className="preview-caption"><span>BUILT AROUND THE EVIDENCE</span><span>DISCOVER → CAPTURE → VERIFY</span></div>
        </div>
      </section>

      <section className="workspace-totals" aria-label="Research workspace totals">
        <div className="totals-intro"><span>YOUR INTELLIGENCE FILE</span><small>{loading ? 'Updating workspace' : `${num(history.length)} saved studies`}</small></div>
        <div className="total-item"><strong>{health ? num(offers) : '—'}</strong><span>Offers captured</span></div>
        <div className="total-item"><strong>{metrics ? num(products) : '—'}</strong><span>Products observed</span></div>
        <div className="total-item"><strong>{metrics ? num(regions) : '—'}</strong><span>Regions covered</span></div>
        <button className="totals-refresh" onClick={() => setRefreshKey((key) => key + 1)} disabled={loading} aria-label="Refresh workspace totals"><Icon name="refresh" size={15} /></button>
      </section>
      {metricsError && <p className="data-warning" role="status">Workspace totals could not be loaded. Refresh to try again.</p>}

      <section className="research-paths">
        <div className="section-heading"><div><span className="eyebrow">CHOOSE YOUR ENTRY POINT</span><h2>Start with the question you have.</h2></div></div>
        <div className="path-grid">
          <button className="path-card path-card-compare" onClick={() => onNavigate('run')}>
            <span className="path-symbol"><Icon name="target" size={19} /></span><span className="path-arrow"><Icon name="arrow" size={16} /></span>
            <small>COMPETITIVE STUDY</small><strong>Compare a company<br />with its rivals.</strong><p>Track offers, regions, and pricing across named competitors.</p>
          </button>
          <button className="path-card path-card-deep" onClick={() => onNavigate('deep')}>
            <span className="path-symbol"><Icon name="compass" size={19} /></span><span className="path-arrow"><Icon name="arrow" size={16} /></span>
            <small>DEEP RESEARCH</small><strong>Investigate a market<br />question.</strong><p>Start from a topic or URL. Get cited findings and clear evidence gaps.</p>
          </button>
        </div>
      </section>

      <section className="recent-section" id="run-history">
        <div className="section-heading recent-heading"><div><span className="eyebrow">CONTINUITY</span><h2>Recently researched</h2></div><button className="editorial-link" onClick={() => onNavigate('history')}>Open full history <Icon name="arrow" size={14} /></button></div>
        {historyError ? <Empty title="Saved history could not be loaded" text={historyError} action="Retry history" onAction={() => setRefreshKey((key) => key + 1)} /> : loading ? (
          <div className="recent-loading"><span className="archive-pulse" /> Loading saved research…</div>
        ) : history.length ? (
          <div className="recent-ledger">
            {history.slice(0, 4).map((run, index) => {
              const quality = run.kind === 'deep' ? run.coverage : run.metrics;
              const sourceCount = run.kind === 'deep' ? quality?.pages_fetched : quality?.urls_succeeded;
              return <button className="recent-ledger-row" key={`${run.kind}-${run.run_id}`} onClick={() => onOpenRun(run)}>
                <span className="ledger-number">{String(index + 1).padStart(2, '0')}</span><span className="ledger-type">{run.kind === 'deep' ? 'DEEP' : 'COMPARE'}</span>
                <strong>{run.title}</strong><span className="ledger-meta">{num(sourceCount)} sources · {num(run.duration_s)} sec</span>
                <Badge tone={run.status === 'completed' ? 'good' : ['failed', 'interrupted'].includes(run.status) ? 'bad' : 'busy'}>{pretty(run.status)}</Badge><Icon name="arrow" size={15} />
              </button>;
            })}
          </div>
        ) : <div className="recent-empty"><span>No studies in the archive yet.</span><button className="editorial-link" onClick={() => onNavigate('run')}>Start your first study <Icon name="arrow" size={14} /></button></div>}
      </section>
    </>
  );
}

/* ------------------------------------------------------------------ */
/* durable run and conversation archive                                */
/* ------------------------------------------------------------------ */
function ResearchHistory({ onOpenRun, onOpenChat, notify }) {
  const [tab, setTab] = useState('runs');
  const [runs, setRuns] = useState([]);
  const [chats, setChats] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    let live = true;
    setLoading(true);
    setError('');
    Promise.allSettled([
      api('/research/runs?limit=500'),
      api('/research/chats?limit=500'),
    ]).then(([runResult, chatResult]) => {
      if (!live) return;
      if (runResult.status === 'fulfilled') setRuns(runResult.value || []);
      if (chatResult.status === 'fulfilled') setChats(chatResult.value || []);
      if (runResult.status === 'rejected' && chatResult.status === 'rejected') {
        setError(`History could not be loaded: ${runResult.reason?.message || chatResult.reason?.message || 'API unavailable'}`);
      } else if (runResult.status === 'rejected' || chatResult.status === 'rejected') {
        notify(`Some history could not be loaded: ${runResult.status === 'rejected' ? runResult.reason?.message : chatResult.reason?.message}`);
      }
    }).finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [refreshKey]);

  const matchingRuns = runs.filter((run) => `${run.title} ${run.kind} ${run.status}`.toLowerCase().includes(query.toLowerCase()));
  const matchingChats = chats.filter((chat) => `${chat.title} ${chat.last_message} ${chat.run_id}`.toLowerCase().includes(query.toLowerCase()));
  const runById = new Map(runs.map((run) => [run.run_id, run]));

  return (
    <>
      <Masthead
        eyebrow="Workspace archive"
        title="Your research, kept together"
        lede="Every saved run and conversation in one place. Reopen a run to inspect its evidence, or return to a chat and carry on where you left off."
        actions={<button className="btn-ghost" onClick={() => setRefreshKey((key) => key + 1)} disabled={loading}><Icon name="refresh" size={14} /> Refresh archive</button>}
      />
      <section className="history-archive panel">
        <div className="archive-toolbar">
          <div className="archive-tabs" role="tablist" aria-label="History type">
            <button role="tab" aria-selected={tab === 'runs'} className={tab === 'runs' ? 'is-active' : ''} onClick={() => setTab('runs')}>Research runs <span>{num(runs.length)}</span></button>
            <button role="tab" aria-selected={tab === 'chats'} className={tab === 'chats' ? 'is-active' : ''} onClick={() => setTab('chats')}>Conversations <span>{num(chats.length)}</span></button>
          </div>
          <label className="archive-search"><Icon name="search" size={15} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={tab === 'runs' ? 'Find a company or run…' : 'Find a conversation…'} aria-label="Search history" /></label>
        </div>
        {error ? <Empty title="History is temporarily unavailable" text={error} action="Try again" onAction={() => setRefreshKey((key) => key + 1)} /> : loading ? (
          <div className="archive-loading"><span className="archive-pulse" /> Loading your saved research…</div>
        ) : tab === 'runs' ? (
          matchingRuns.length ? (
            <div className="archive-list">
              {matchingRuns.map((run) => {
                const metrics = run.kind === 'deep' ? run.coverage : run.metrics;
                const sourceCount = run.kind === 'deep' ? metrics?.pages_fetched : metrics?.urls_succeeded;
                return (
                  <button className="archive-row" key={`${run.kind}-${run.run_id}`} onClick={() => onOpenRun(run)}>
                    <span className={`archive-icon ${run.kind === 'deep' ? 'archive-icon-deep' : ''}`}><Icon name={run.kind === 'deep' ? 'compass' : 'target'} size={17} /></span>
                    <span className="archive-row-main"><strong>{run.title}</strong><small>{run.kind === 'deep' ? 'Deep research' : 'Competitive research'} · {run.created_at ? new Date(run.created_at).toLocaleString() : 'Date unavailable'} · {short(run.run_id, 18)}</small></span>
                    <span className="archive-row-detail"><strong>{num(sourceCount)} <small>sources fetched</small></strong><small>{num(metrics?.llm_calls)} LLM calls · {metrics?.estimated_tokens ? `~${num(metrics.estimated_tokens)} tokens` : 'token usage unavailable'}</small></span>
                    <Badge tone={run.status === 'completed' ? 'good' : ['failed', 'interrupted'].includes(run.status) ? 'bad' : 'busy'}>{pretty(run.status)}</Badge>
                    <Icon name="arrow" size={15} />
                  </button>
                );
              })}
            </div>
          ) : <Empty title={query ? 'No matching runs' : 'No saved runs yet'} text={query ? 'Try a different company or search term.' : 'Completed and in-progress research will appear here automatically.'} />
        ) : matchingChats.length ? (
          <div className="archive-list">
            {matchingChats.map((chat) => {
              const relatedRun = runById.get(chat.run_id);
              return (
                <button className="archive-row archive-chat-row" key={chat.session_id} onClick={() => onOpenChat(chat)}>
                  <span className="archive-icon archive-icon-chat"><Icon name="chat" size={17} /></span>
                  <span className="archive-row-main"><strong>{chat.title || 'Research conversation'}</strong><small>{relatedRun?.title || 'Saved research run'} · {chat.updated_at ? `Updated ${new Date(chat.updated_at).toLocaleString()}` : 'Date unavailable'}</small><span className="archive-preview">{short(chat.last_message, 140)}</span></span>
                  <span className="archive-row-detail"><strong>{num(Math.floor((chat.message_count || 0) / 2))} <small>questions</small></strong><small>Conversation saved</small></span>
                  <span className="archive-continue">Continue <Icon name="arrow" size={14} /></span>
                </button>
              );
            })}
          </div>
        ) : <Empty title={query ? 'No matching conversations' : 'No saved conversations yet'} text={query ? 'Try another title or phrase.' : 'Ask a question from any completed run. Your conversation will be saved here and can be continued later.'} />}
      </section>
    </>
  );
}

/* ------------------------------------------------------------------ */
/* research pipeline visualization                                     */
/* ------------------------------------------------------------------ */

// Stage keys must match the event names the orchestrator actually emits,
// otherwise no stage can ever light up.
const PIPELINE_STAGES = [
  { key: 'discovery_complete', label: 'Discovery', desc: 'Finding candidate URLs' },
  { key: 'region_fanout_ready', label: 'Fan-out', desc: 'Applying region context' },
  { key: 'scrape_started', label: 'Scraping', desc: 'Fetching page content' },
  { key: 'scrape_succeeded', label: 'Extraction', desc: 'Structured extraction' },
  { key: 'extraction_succeeded', label: 'Validation', desc: 'Schema and semantic checks' },
  { key: 'run_completed', label: 'Insights', desc: 'Saving observations' },
];

const STAGE_ORDER = PIPELINE_STAGES.map((stage) => stage.key);

function ResearchPipeline({ status, events }) {
  const seen = new Set((events || []).map((event) => event.event));
  const stageSeen = (key) => key === 'extraction_succeeded'
    ? ['extraction_succeeded', 'extraction_skipped', 'extraction_failed'].some((event) => seen.has(event))
    : seen.has(key);
  const matchedStage = STAGE_ORDER.findIndex(stageSeen);
  const reached = matchedStage < 0 ? 0 : matchedStage;
  const finished = status === 'completed' || status === 'failed';

  const counts = useMemo(() => {
    const scraped = (events || []).filter((e) => e.event === 'scrape_succeeded').length;
    const extracted = (events || []).filter((e) => e.event === 'extraction_succeeded').length;
    const failed = (events || []).filter((e) => e.event === 'extraction_failed').length;
    const skipped = (events || []).filter((e) => e.event === 'extraction_skipped').length;
    return { scraped, extracted, failed, skipped };
  }, [events]);

  return (
    <section className="pipeline-viz" aria-label="Research pipeline progress">
      <div className="pipeline-header">
        <h3>Pipeline progress</h3>
        <span className="pipeline-counts">
          {counts.scraped} scraped · {counts.extracted} extracted
          {counts.skipped ? ` · ${counts.skipped} skipped (no priced offer)` : ''}
          {counts.failed ? ` · ${counts.failed} extraction errors` : ''}
        </span>
      </div>

      <ol className="pipeline-track">
        {PIPELINE_STAGES.map((stage, index) => {
          const done = stageSeen(stage.key);
          const current = !finished && index === reached;
          const past = reached > index;
          const state = done || past ? 'complete' : current ? 'current' : 'pending';
          return (
            <li key={stage.key} className={`pipeline-stage is-${state}`}>
              <span className="pipeline-dot">
                {state === 'complete' ? <Icon name="check" size={12} /> : <span className="dot-inner" />}
              </span>
              <span className="stage-label">{stage.label}</span>
              <span className="stage-desc">{stage.desc}</span>
            </li>
          );
        })}
      </ol>

      <ol className="pipeline-events">
        {(events || []).slice(-4).reverse().map((event, index) => (
          <li className={`event-row${event.event === 'extraction_failed' ? ' event-bad' : event.event === 'extraction_skipped' ? ' event-skipped' : ''}`} key={`${event.timestamp}-${index}`}>
            <span className="event-time mono">{new Date(event.timestamp).toLocaleTimeString()}</span>
            <span className="event-name">{event.event === 'extraction_skipped' ? 'Skipped · no priced offer' : event.event === 'extraction_started' ? 'Extracting offer · waiting for model' : event.event === 'review_extraction_completed' ? `Review scan complete · ${num(event.reviews_found)} reviews` : event.event === 'discovery_entity_started' ? `Finding pages · ${event.company}` : event.event === 'discovery_entity_complete' ? `Pages checked · ${event.company}` : pretty(event.event)}</span>
            {(event.reason || event.error || event.product_name || event.website) && <small className="event-detail">{event.reason || event.error || event.product_name || event.website}</small>}
            {event.url && (
              <a href={event.url} target="_blank" rel="noreferrer" className="event-url mono">
                {short(event.url, 44)}
              </a>
            )}
          </li>
        ))}
      </ol>
    </section>
  );
}

/* ------------------------------------------------------------------ */
/* competitive run                                                     */
/* ------------------------------------------------------------------ */
function CompetitiveRun({ onStart, runId, onClearRun, onViewEvidence, onAsk, notify }) {
  const [company, setCompany] = useState('');
  const [website, setWebsite] = useState('');
  const [products, setProducts] = useState('');
  const [competitors, setCompetitors] = useState('');
  const [competitorUrls, setCompetitorUrls] = useState('');
  const [regions, setRegions] = useState(['US']);
  const [job, setJob] = useState(null);
  const [elapsed, setElapsed] = useState(0);
  const [loadError, setLoadError] = useState('');

  // The run id lives in the parent. Without this, `job` stayed null forever,
  // the status panel showed "No run started" and the pipeline never rendered,
  // even though the backend had already accepted and was running the job.
  useEffect(() => {
    if (!runId) {
      setJob(null);
      setLoadError('');
      return undefined;
    }
    let cancelled = false;
    api(`/research/jobs/${runId}`)
      .then((data) => {
        if (!cancelled) {
          setJob(data);
          setLoadError('');
        }
      })
      .catch((error) => {
        if (!cancelled) {
          setJob(null);
          if (/job not found/i.test(error.message)) {
            const message = 'This run is no longer available on the API. It may have been created before durable history was configured or lost when the service restarted. Start a new run to continue.';
            setLoadError(message);
          } else setLoadError(error.message);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [runId]);

  // Poll only while the job is actually in flight.
  useEffect(() => {
    if (!job || ['completed', 'failed', 'interrupted'].includes(job.status)) return undefined;
    const timer = setInterval(() => {
      api(`/research/jobs/${job.job_id}`)
        .then((data) => {
          setJob(data);
          setLoadError('');
        })
        .catch((error) => {
          if (/job not found/i.test(error.message)) {
            setJob(null);
            setLoadError('The API no longer has this run. It may have restarted without persistent history, or this browser is showing a stale run. Start a new run; it will stop polling this missing job.');
          } else setLoadError(error.message);
        });
    }, 2000);
    return () => clearInterval(timer);
  }, [job]);

  // A visible clock, so a slow run never looks like a dead one.
  useEffect(() => {
    if (!job || ['completed', 'failed', 'interrupted'].includes(job.status)) return undefined;
    const startedAt = job.started_at ? Date.parse(job.started_at) : Date.now();
    setElapsed(Math.max(0, Math.round((Date.now() - startedAt) / 1000)));
    const timer = setInterval(() => {
      setElapsed(Math.max(0, Math.round((Date.now() - startedAt) / 1000)));
    }, 1000);
    return () => clearInterval(timer);
  }, [job]);

  const toggleRegion = (region) =>
    setRegions((current) =>
      current.includes(region) ? current.filter((r) => r !== region) : [...current, region],
    );

  const submit = (event) => {
    event.preventDefault();
    const competitorNames = competitors.split(',').map((c) => c.trim()).filter(Boolean);
    const competitorUrlsList = competitorUrls.split(',').map((u) => u.trim()).filter(Boolean);
    if (competitorNames.length && competitorNames.length !== competitorUrlsList.length) {
      notify?.('Add a website for each competitor, in the same order as the names. Guessed .com URLs are not used.');
      return;
    }
    const competitorsData = competitorNames.map((name, i) => ({
      name,
      website: competitorUrlsList[i],
    }));
    onStart({
      company,
      website,
      regions,
      focus_products: products.split(',').map((p) => p.trim()).filter(Boolean),
      competitors: competitorsData,
    });
  };

  return (
    <>
      <Masthead
        eyebrow="Competitive run"
        title="Track a company and its rivals"
        lede="Structured extraction across regions, with every observation kept alongside the page it came from."
      />

      <div className="split">
        <section className="panel">
          <form className="form" onSubmit={submit}>
            <label className="field">
              <span>Company</span>
              <input required value={company} onChange={(e) => setCompany(e.target.value)} placeholder="Acme Audio" />
            </label>
            <label className="field">
              <span>Website</span>
              <input required type="url" value={website} onChange={(e) => setWebsite(e.target.value)} placeholder="https://acme.com" />
            </label>
            <label className="field">
              <span>Focus products or category</span>
              <input value={products} onChange={(e) => setProducts(e.target.value)} placeholder="Pro X Headphones, Air Buds Lite" />
              <small>A hint for discovery. If the site uses different URL names, the top page for that company is still checked.</small>
            </label>
            <label className="field">
              <span>Competitors</span>
              <input value={competitors} onChange={(e) => setCompetitors(e.target.value)} placeholder="Obsidian, Coda, Roam Research" />
              <small>Comma separated names.</small>
            </label>
            <label className="field">
              <span>Competitor websites</span>
              <input value={competitorUrls} onChange={(e) => setCompetitorUrls(e.target.value)} placeholder="https://obsidian.md, https://coda.io, https://roamresearch.com" />
              <small>Comma separated URLs, in the same order as the names. Required whenever competitor names are entered.</small>
            </label>

            <fieldset className="field">
              <span>Regions</span>
              <div className="chips">
                {REGIONS.map((region) => (
                  <button
                    key={region}
                    type="button"
                    className={`chip${regions.includes(region) ? ' chip-on' : ''}`}
                    onClick={() => toggleRegion(region)}
                  >
                    {region}
                  </button>
                ))}
              </div>
            </fieldset>

            <button className="btn-primary" type="submit">
              Start run
            </button>
          </form>
        </section>

        <section className="panel">
          <div className="panel-head">
            <h2>Run status</h2>
            <div className="panel-head-right">
              {runId && <span className="mono">{runId}</span>}
              {job && ['completed', 'failed', 'interrupted'].includes(job.status) && (
                <button
                  className="btn-ghost"
                  onClick={() => api(`/research/jobs/${runId}`).then(setJob).catch((e) => setLoadError(e.message))}
                >
                  <Icon name="refresh" size={14} />
                  Refresh
                </button>
              )}
              {job && ['queued', 'running'].includes(job.status) && (
                <button className="btn-ghost" onClick={onClearRun} title="Stop viewing this run; the API worker continues in the background">
                  Hide run
                </button>
              )}
            </div>
          </div>

          {loadError && (
            <div className="run-load-error" role="alert">
              <p className="error-text">{loadError}</p>
              {runId && /no longer (available|has this run)/i.test(loadError) && (
                <button className="btn-primary" onClick={onClearRun}>
                  Start a new run
                </button>
              )}
            </div>
          )}

          {job ? (
            <>
              <ResearchPipeline status={job.status} events={job.events} />
              <div className="run-state">
                <Badge tone={['failed', 'interrupted'].includes(job.status) ? 'bad' : job.status === 'completed' ? 'good' : 'busy'}>
                  {job.status}
                </Badge>
                {!['completed', 'failed', 'interrupted'].includes(job.status) && (
                  <span className="mono run-clock">{elapsed}s elapsed</span>
                )}
                <p className="muted">
                  {job.current_event?.event
                    ? `${job.current_event.event === 'extraction_started' ? 'Waiting for product extraction response' : job.current_event.event === 'discovery_entity_started' ? `Finding pages for ${job.current_event.company}` : pretty(job.current_event.event)}${job.current_event.url ? ` · ${job.current_event.url}` : ''}`
                    : 'Waiting for the worker to pick this up.'}
                </p>
                {job.error && <p className="error-text">{job.error}</p>}
                {job.status === 'failed' && !job.error && (
                  <p className="muted">
                    The run failed without a reported reason. Check the API service logs for the
                    provider error — free tiers usually answer 429 here.
                  </p>
                )}
                {job.result && (
                  <dl className="facts">
                    <div><dt>Runtime</dt><dd className="mono">{num(job.result.duration_s)}s</dd></div>
                    <div><dt>Source pages</dt><dd className="mono">{num(job.result.metrics?.urls_succeeded)} fetched · {num(job.result.metrics?.urls_failed)} failed</dd></div>
                    <div><dt>Extraction</dt><dd className="mono">{num(job.result.metrics?.extraction_successes)} passed · {num(job.result.metrics?.extraction_failures)} failed · {num(job.result.metrics?.extractions_skipped)} skipped</dd></div>
                    <div><dt>Compute</dt><dd className="mono">{num(job.result.metrics?.llm_calls)} LLM calls · {job.result.metrics?.estimated_tokens ? `~${num(job.result.metrics.estimated_tokens)} estimated tokens` : job.result.metrics?.llm_calls ? 'token estimate unavailable' : '0 estimated tokens'}</dd></div>
                    {job.result.offers_captured != null && (
                      <div><dt>Offers captured</dt><dd className="mono">{num(job.result.offers_captured)}</dd></div>
                    )}
                    {job.result.runs != null && (
                      <div><dt>Runs recorded</dt><dd className="mono">{num(job.result.runs)}</dd></div>
                    )}
                    {job.result.reviews != null && (
                      <div><dt>Reviews stored</dt><dd className="mono">{num(job.result.reviews)}</dd></div>
                    )}
                    {job.result.extractions_skipped > 0 && (
                      <div><dt>Pages without priced offers</dt><dd className="mono">{num(job.result.extractions_skipped)}</dd></div>
                    )}
                  </dl>
                )}
                {job.result?.metrics?.errors?.length > 0 && (
                  <div className="run-notes" role="status">
                    <strong>Research notes</strong>
                    <ul>{job.result.metrics.errors.slice(0, 5).map((message, index) => <li key={index}>{message}</li>)}</ul>
                  </div>
                )}
                {job.status === 'completed' && (
                  <div className="run-actions">
                    <button className="btn-ghost" onClick={onViewEvidence}><Icon name="layers" size={15} /> View captured evidence</button>
                    <button className="btn-primary" onClick={() => onAsk(runId)} disabled={job.chat_ready === false}>
                      <Icon name="chat" size={15} /> Ask this run
                    </button>
                    {job.chat_error && <p className="error-text">Run completed, but chat indexing failed: {job.chat_error}</p>}
                  </div>
                )}
              </div>
            </>
          ) : runId && !/no longer (available|has this run)/i.test(loadError) ? (
            <Empty
              title="Loading run status"
              text={`Fetching ${runId} from the API…`}
            />
          ) : (
            <Empty
              title="No run started"
              text="Fill in the form to capture structured offers for a target company."
            />
          )}
        </section>
      </div>
    </>
  );
}

/* ------------------------------------------------------------------ */
/* deep research                                                       */
/* ------------------------------------------------------------------ */
const DEEP_MODES = [
  ['url', 'A single site'],
  ['topic', 'A topic'],
  ['compare', 'A comparison'],
];

const DEEP_TABS = [
  ['report', 'Report'],
  ['findings', 'Findings'],
  ['gaps', 'Gaps'],
  ['pages', 'Pages visited'],
  ['sources', 'Sources'],
];

function DeepResearch({ result, onResult, onChat, notify, health, onNeedKey }) {
  const [mode, setMode] = useState('url');
  const [url, setUrl] = useState('');
  const [topic, setTopic] = useState('');
  const [rivals, setRivals] = useState('');
  const [question, setQuestion] = useState('');
  const [pages, setPages] = useState(8);
  const [useSearch, setUseSearch] = useState(true);
  const [tab, setTab] = useState('report');
  const [busy, setBusy] = useState(false);

  const submit = async (event) => {
    event.preventDefault();
    setBusy(true);
    try {
      const payload = {
        mode,
        url: mode === 'url' ? url.trim() : null,
        topic: mode === 'topic' ? topic.trim() : null,
        competitors: mode === 'compare' ? rivals.split(',').map((c) => c.trim()).filter(Boolean) : [],
        question: question.trim(),
        max_pages: pages,
        use_search: useSearch,
      };
      const data = await api('/research/deep', { method: 'POST', body: JSON.stringify(payload) });
      onResult(data);
      setTab('report');
    } catch (error) {
      if (error.code === 'auth') onNeedKey?.();
      notify(error.message);
    } finally {
      setBusy(false);
    }
  };

  const coverage = result?.coverage;
  const searchLive = health?.deep_research?.search !== false;

  return (
    <>
      <Masthead
        eyebrow="Deep research"
        title="Crawl a site and read it properly"
        lede="Pages are parsed with their heading structure, values are extracted, and every claim in the report points back to the page it came from."
      />

      <section className="panel panel-form">
        <div className="segmented" role="tablist" aria-label="Research mode">
          {DEEP_MODES.map(([id, label]) => (
            <button
              key={id}
              role="tab"
              aria-selected={mode === id}
              className={mode === id ? 'is-active' : ''}
              onClick={() => setMode(id)}
            >
              {label}
            </button>
          ))}
        </div>

        {!searchLive && (mode === 'topic' || mode === 'compare') && (
          <p className="data-warning" role="status">
            Web search is off on the API, so topic and comparison runs cannot discover competitor sites. Set
            <span className="mono"> DEEP_SEARCH_ENABLED=true</span> on the Northflank Environment page and redeploy.
          </p>
        )}

        <form className="form form-grid" onSubmit={submit}>
          {mode === 'url' && (
            <label className="field">
              <span>Site URL</span>
              <input required type="url" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://acme.com" />
            </label>
          )}

          {mode === 'topic' && (
            <label className="field">
              <span>Topic</span>
              <input required value={topic} onChange={(e) => setTopic(e.target.value)} placeholder="AI note-taking app pricing" />
            </label>
          )}

          {mode === 'compare' && (
            <>
              <label className="field">
                <span>Subject</span>
                <input required value={topic} onChange={(e) => setTopic(e.target.value)} placeholder="Notion" />
              </label>
              <label className="field">
                <span>Competitors</span>
                <input required value={rivals} onChange={(e) => setRivals(e.target.value)} placeholder="Coda, Obsidian, Anytype" />
                <small>Comma separated.</small>
              </label>
            </>
          )}

          <label className="field field-wide">
            <span>What should the research answer?</span>
            <input
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder="What does it charge, what limits apply, and what are the trial terms?"
            />
            <small>Leave empty to let the planner derive its own questions.</small>
          </label>

          <div className="field field-wide controls">
            <label className="slider">
              <span>
                Page budget <strong className="mono">{pages}</strong>
              </span>
              <input type="range" min="2" max="30" value={pages} onChange={(e) => setPages(Number(e.target.value))} />
            </label>
            <label className="check">
              <input type="checkbox" checked={useSearch} onChange={(e) => setUseSearch(e.target.checked)} />
              <span>Search the web for extra sources</span>
            </label>
          </div>

          <div className="field-wide">
            <button className={`btn-primary${busy ? ' is-busy' : ''}`} disabled={busy}>
              {busy ? 'Researching…' : 'Start deep research'}
            </button>
            <small className="muted">This run waits on the API. A larger page budget can take several minutes.</small>
          </div>
        </form>
      </section>

      {result && (
        <>
          <section className="stat-row" aria-label="Coverage">
            <Stat label="Status" value={pretty(result.status)} note={`${result.duration_s}s elapsed`} />
            <Stat label="Source quality" value={`${num(coverage?.pages_fetched)} fetched`} note={`${num(coverage?.pages_failed)} failed · ${num(coverage?.pages_skipped)} skipped`} />
            <Stat label="Sections" value={num(coverage?.sections)} note={`${num(coverage?.facts)} facts extracted`} />
            <Stat label="Run usage" value={`${num(coverage?.llm_calls)} calls`} note={`${coverage?.estimated_tokens ? `~${num(coverage.estimated_tokens)} estimated tokens` : coverage?.llm_calls ? 'token estimate unavailable' : '0 estimated tokens'} · ${num(coverage?.characters)} evidence chars`} />
          </section>

          <section className="panel">
            <div className="panel-head">
              <div className="tabs" role="tablist">
                {DEEP_TABS.map(([id, label]) => (
                  <button
                    key={id}
                    role="tab"
                    aria-selected={tab === id}
                    className={tab === id ? 'is-active' : ''}
                    onClick={() => setTab(id)}
                  >
                    {label}
                    {id === 'gaps' && result.gaps?.length ? <em>{result.gaps.length}</em> : null}
                    {id === 'sources' && result.citations?.length ? <em>{result.citations.length}</em> : null}
                  </button>
                ))}
              </div>
              <button className="btn-ghost" onClick={onChat}>
                <Icon name="chat" size={15} />
                Ask about this run
              </button>
            </div>

            {tab === 'report' &&
              (result.report_markdown ? (
                <Prose markdown={result.report_markdown} />
              ) : (
                <Empty title="No report produced" text="The run finished without usable evidence." />
              ))}

            {tab === 'findings' &&
              (result.findings?.length ? (
                <ul className="bullets">
                  {result.findings.map((finding, index) => (
                    <li key={index}>
                      <Inline text={String(finding)} />
                    </li>
                  ))}
                </ul>
              ) : (
                <Empty title="No findings" text="No evidence-backed statements were captured." />
              ))}

            {tab === 'gaps' &&
              (result.gaps?.length ? (
                <ul className="gaps">
                  {result.gaps.map((gap, index) => (
                    <li key={index}>
                      <strong>{gap.question}</strong>
                      <p>{gap.reason}</p>
                    </li>
                  ))}
                </ul>
              ) : (
                <Empty title="No open questions" text="Every planned question found supporting evidence." />
              ))}

            {tab === 'pages' &&
              (result.pages?.length ? (
                <table className="table">
                  <thead>
                    <tr>
                      <th scope="col">Ref</th>
                      <th scope="col">URL</th>
                      <th scope="col">Type</th>
                      <th scope="col">Status</th>
                      <th scope="col" className="align-right">Chars</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.pages.map((page) => (
                      <tr key={page.url}>
                        <td><span className={`ref ref-${citeTone(page.status)}`}>{page.source_ref}</span></td>
                        <td>
                          <a href={page.url} target="_blank" rel="noreferrer">
                            {short(page.url, 60)}
                            <Icon name="external" size={12} />
                          </a>
                        </td>
                        <td>{pretty(page.page_type)}</td>
                        <td><Badge tone={page.status === 'fetched' ? 'good' : page.status === 'failed' ? 'bad' : 'neutral'}>{page.status}</Badge></td>
                        <td className="align-right mono">{num(page.chars)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <Empty title="No pages visited" text="The crawl frontier stayed empty." />
              ))}

            {tab === 'sources' &&
              (result.citations?.length ? (
                <ol className="sources">
                  {result.citations.map((citation) => (
                    <li key={citation.ref}>
                      <span className="ref">{citation.ref}</span>
                      <a href={citation.url} target="_blank" rel="noreferrer">
                        <strong>{citation.title || citation.url}</strong>
                        <small>{citation.heading || citation.url}</small>
                      </a>
                      <Icon name="external" size={14} />
                    </li>
                  ))}
                </ol>
              ) : (
                <Empty title="No sources" text="Nothing citable was captured." />
              ))}
          </section>
        </>
      )}
    </>
  );
}

/* ------------------------------------------------------------------ */
/* evidence                                                            */
/* ------------------------------------------------------------------ */
function Evidence({ runId, notify }) {
  const [rows, setRows] = useState([]);
  const [sources, setSources] = useState([]);
  const [sourceText, setSourceText] = useState('');
  const [sourceLoading, setSourceLoading] = useState(false);
  const [search, setSearch] = useState('');
  const [region, setRegion] = useState('');
  const [selectedKey, setSelectedKey] = useState('');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    const params = new URLSearchParams({ limit: '200' });
    if (region) params.set('region', region);
    if (search) params.set('product', search);
    if (runId) params.set('run_id', runId);

    const offersRequest = api(`/offers?${params}`);
    const sourcesRequest = runId ? api(`/evidence/${encodeURIComponent(runId)}?limit=200`) : Promise.resolve([]);
    Promise.allSettled([offersRequest, sourcesRequest]).then(([offersResult, sourcesResult]) => {
      if (offersResult.status === 'fulfilled') setRows(offersResult.value);
      else notify(offersResult.reason.message);
      if (sourcesResult.status === 'fulfilled') setSources(sourcesResult.value);
      else setSources([]);
    }).finally(() => setLoading(false));
  }, [region, search, runId]);

  const items = useMemo(() => [
    ...sources.map((source) => ({
      kind: 'source',
      key: `source:${source.source_url}:${source.content_hash || ''}`,
      title: source.source_url,
      subtitle: `${num(source.size)} chars captured`,
      url: source.source_url,
      source,
    })),
    ...rows.map((offer, index) => ({
      kind: 'offer',
      key: `offer:${offer.run_id || runId || 'all'}:${offer.url || index}:${offer.product_name}`,
      title: offer.product_name || 'Captured offer',
      subtitle: `${offer.competitor || offer.brand || 'Company not identified'} · ${offer.region || 'Region not identified'} · ${money(offer.price, offer.currency)}`,
      url: offer.url,
      offer,
    })),
  ], [rows, sources, runId]);
  const selectedItem = items.find((item) => item.key === selectedKey) || items[0] || null;

  const openSource = async (source) => {
    setSourceText('');
    setSourceLoading(true);
    try {
      const query = new URLSearchParams({ source_url: source.source_url, run_id: source.run_id || runId || '' });
      const data = await api(`/evidence/content?${query}`);
      setSourceText(data.content || 'No captured page text was saved.');
    } catch (error) {
      notify(error.message);
      setSourceText('The saved page content could not be loaded.');
    } finally {
      setSourceLoading(false);
    }
  };

  const openOfferSource = async (offer) => {
    if (!offer?.url) return;
    const source = sources.find((item) => item.source_url === offer.url);
    if (source) return openSource(source);
    setSourceLoading(true);
    setSourceText('');
    try {
      const query = new URLSearchParams({ source_url: offer.url, run_id: offer.run_id || runId || '' });
      const data = await api(`/evidence/content?${query}`);
      setSourceText(data.content || 'No captured page text was saved for this observation.');
    } catch (error) {
      notify(error.message);
      setSourceText('The saved page content could not be loaded.');
    } finally {
      setSourceLoading(false);
    }
  };

  return (
    <>
      <Masthead
        eyebrow="SOURCE LIBRARY"
        title="Evidence, in context."
        lede="Every captured offer stays beside the page it came from, with the price, region, and extraction detail ready to inspect."
      />

      <section className="evidence-workspace" aria-label="Captured source library">
        <aside className="evidence-library">
          <div className="evidence-library-toolbar">
            <label className="evidence-search"><Icon name="search" size={15} /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search evidence…" aria-label="Search captured evidence" /></label>
            <label className="evidence-region"><span>REGION</span><select value={region} onChange={(event) => setRegion(event.target.value)} aria-label="Filter by region"><option value="">All regions</option>{REGIONS.map((item) => <option key={item} value={item}>{item}</option>)}</select></label>
            <div className="evidence-count">{loading ? 'Loading evidence…' : `${items.length} captured items`}</div>
          </div>
          <div className="evidence-item-list" role="listbox" aria-label="Captured evidence">
            {loading ? <div className="evidence-loading"><span /><span /><span /></div> : items.length ? items.map((item) => (
              <button key={item.key} role="option" aria-selected={(selectedItem?.key === item.key)} className={`evidence-item${selectedItem?.key === item.key ? ' is-selected' : ''}`} onClick={() => { setSelectedKey(item.key); setSourceText(''); }}>
                <span className="evidence-item-kind">{item.kind === 'source' ? 'SOURCE PAGE' : 'CAPTURED OFFER'}</span>
                <strong>{item.kind === 'source' ? short(item.title, 66) : item.title}</strong>
                <small>{item.subtitle}</small>
                {item.kind === 'offer' && <span className="evidence-item-price">{money(item.offer.price, item.offer.currency)}</span>}
              </button>
            )) : <div className="evidence-list-empty"><strong>No evidence found</strong><p>Change the search or run competitive research to capture source pages and offers.</p></div>}
          </div>
        </aside>
        <article className="evidence-detail" aria-live="polite">
          {!selectedItem ? <Empty title="Your evidence will appear here" text="Select a captured source or offer to inspect its details." /> : selectedItem.kind === 'source' ? (
            <>
              <div className="evidence-detail-top"><Badge tone="good">SOURCE CAPTURED</Badge><span className="mono">{selectedItem.source.scraped_at ? new Date(selectedItem.source.scraped_at).toLocaleString() : 'Capture time unavailable'}</span></div>
              <h2>{short(selectedItem.source.source_url, 110)}</h2>
              <p className="evidence-detail-lede">A source page fetched for this research. The captured text is retained so findings can be traced to the original evidence.</p>
              <div className="evidence-contribution"><div><span>CAPTURED CONTENT</span><strong>{num(selectedItem.source.size)} characters</strong></div><div><span>CONTENT HASH</span><strong className="mono">{short(selectedItem.source.content_hash, 28)}</strong></div></div>
              <div className="evidence-detail-actions"><button className="btn-primary" onClick={() => openSource(selectedItem.source)}><Icon name="doc" size={15} /> Inspect captured text</button><a className="btn-ghost" href={selectedItem.source.source_url} target="_blank" rel="noreferrer">Open original page <Icon name="external" size={14} /></a></div>
              {sourceText && <div className="evidence-captured-text"><div className="evidence-text-heading"><strong>Captured page text</strong>{sourceLoading && <small>Loading…</small>}</div><pre className="source-content">{sourceText}</pre></div>}
            </>
          ) : (
            <>
              <div className="evidence-detail-top"><Badge tone="good">CAPTURED OFFER</Badge><span className="mono">{selectedItem.offer.scraped_at ? new Date(selectedItem.offer.scraped_at).toLocaleString() : 'Capture time unavailable'}</span></div>
              <div className="evidence-offer-heading"><div><span className="eyebrow">{selectedItem.offer.competitor || selectedItem.offer.brand || 'Company not identified'}</span><h2>{selectedItem.offer.product_name}</h2></div><strong>{money(selectedItem.offer.price, selectedItem.offer.currency)}</strong></div>
              <p className="evidence-detail-lede">This offer was extracted from a fetched competitor page. Verify the original listing before using it in a decision.</p>
              <div className="evidence-contribution"><div><span>COMPANY</span><strong>{selectedItem.offer.competitor || selectedItem.offer.brand || 'Not identified'}</strong></div><div><span>REGION</span><strong>{selectedItem.offer.region || 'Not specified'}</strong></div><div><span>AVAILABILITY</span><strong>{pretty(selectedItem.offer.availability) || 'Unknown'}</strong></div><div><span>EXTRACTION CONFIDENCE</span><strong>{selectedItem.offer.extraction_confidence != null ? `${Math.round(selectedItem.offer.extraction_confidence * 100)}%` : 'Not reported'}</strong></div></div>
              <div className="evidence-detail-source"><span>SOURCE URL</span><a href={selectedItem.offer.url} target="_blank" rel="noreferrer">{selectedItem.offer.url || 'No source URL saved'} <Icon name="external" size={14} /></a></div>
              <div className="evidence-detail-actions">{selectedItem.offer.url && <button className="btn-primary" onClick={() => openOfferSource(selectedItem.offer)}><Icon name="doc" size={15} /> {sourceLoading ? 'Loading source…' : 'Inspect captured text'}</button>}</div>
              {sourceText && <div className="evidence-captured-text"><div className="evidence-text-heading"><strong>Captured page text</strong>{sourceLoading && <small>Loading…</small>}</div><pre className="source-content">{sourceText}</pre></div>}
            </>
          )}
        </article>
      </section>
    </>
  );
}

/* ------------------------------------------------------------------ */
/* signals                                                             */
/* ------------------------------------------------------------------ */
function Signals({ runId }) {
  const [data, setData] = useState({ undercut: [], moves: [], regional: [], events: [] });

  useEffect(() => {
    const suffix = runId ? `?run_id=${runId}` : '';
    Promise.all([
      api(`/insights/undercut${suffix}`).catch(() => []),
      api(`/insights/price-moves?days_back=30${runId ? `&run_id=${runId}` : ''}`).catch(() => []),
      api(`/insights/regional${suffix}`).catch(() => []),
      api(`/insights/events${suffix}`).catch(() => []),
    ]).then(([undercut, moves, regional, events]) =>
      setData({ undercut, moves, regional, events }),
    );
  }, [runId]);

  return (
    <>
      <Masthead
        eyebrow="Signals"
        title="What changed"
        lede="Comparisons are only produced when two comparable observations exist, so an alert always has evidence behind it."
      />
      <div className="split">
        <section className="panel">
          <div className="panel-head"><h2>Undercutting</h2></div>
          {data.undercut.length ? (
            <ul className="bullets">
              {data.undercut.slice(0, 10).map((item, index) => (
                <li key={index}><Inline text={JSON.stringify(item)} /></li>
              ))}
            </ul>
          ) : (
            <Empty title="No undercut signals" text="No competitor is currently priced below the target in the same region." />
          )}
        </section>
        <section className="panel">
          <div className="panel-head"><h2>Price movement</h2></div>
          {data.moves.length ? (
            <ul className="bullets">
              {data.moves.slice(0, 10).map((item, index) => (
                <li key={index}><Inline text={JSON.stringify(item)} /></li>
              ))}
            </ul>
          ) : (
            <Empty title="No price movement" text="No comparable re-capture has shown a real change yet." />
          )}
        </section>
      </div>
    </>
  );
}

/* ------------------------------------------------------------------ */
/* research chat                                                       */
/* ------------------------------------------------------------------ */
const STARTERS = [
  'What does it charge per month?',
  'Which limits and guarantees are stated?',
  'What did customers complain about?',
];

function ResearchChat({ runId, sessionId: savedSessionId, notify }) {
  const [runs, setRuns] = useState([]);
  const [activeRun, setActiveRun] = useState(runId || '');
  const [activeSessionId, setActiveSessionId] = useState(savedSessionId || '');
  const [messages, setMessages] = useState([]);
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const sessionId = activeSessionId;

  useEffect(() => {
    api('/research/runs?limit=100')
      .then((items) => {
        const available = items.filter((item) => ['completed', 'partial'].includes(item.status));
        setRuns(available);
        setActiveRun((selected) => {
          if (runId) return runId;
          if (selected && available.some((item) => item.run_id === selected)) return selected;
          return available[0]?.run_id || '';
        });
      })
      .catch((error) => notify(`Saved research runs could not be loaded: ${error.message}`));
  }, []);

  useEffect(() => {
    if (runId) setActiveRun(runId);
  }, [runId]);

  useEffect(() => {
    setActiveSessionId(savedSessionId || '');
  }, [savedSessionId]);

  useEffect(() => {
    if (runId && !savedSessionId) setActiveSessionId('');
  }, [runId, savedSessionId]);

  useEffect(() => {
    if (!sessionId) {
      setMessages([]);
      return;
    }
    api(`/research/chat/${sessionId}`)
      .then((data) =>
        setMessages(
          (data.messages || []).map((m) => ({
            role: m.role,
            content: m.content,
            citations: safeJson(m.citations),
          })),
        ),
      )
      .catch(() => setMessages([]));
  }, [sessionId, activeRun]);

  const send = async (event) => {
    event.preventDefault();
    const question = draft.trim();
    if (!question || busy) return;

    setBusy(true);
    setDraft('');
    setMessages((current) => [...current, { role: 'user', content: question, citations: [] }]);

    try {
      const answer = await api('/research/chat', {
        method: 'POST',
        body: JSON.stringify({
          question,
          run_id: activeRun || null,
          session_id: sessionId || null,
        }),
      });
      if (answer.session_id) setActiveSessionId(answer.session_id);
      setMessages((current) => [
        ...current,
        {
          role: 'assistant',
          content: answer.message,
          citations: answer.citations || [],
          answerable: answer.answerable,
        },
      ]);
    } catch (error) {
      notify(error.message);
    } finally {
      setBusy(false);
    }
  };

  const startNew = () => {
    setActiveSessionId('');
    setMessages([]);
  };

  const current = runs.find((run) => run.run_id === activeRun);

  return (
    <>
      <Masthead
        eyebrow="Research chat"
        title="Interrogate the evidence"
        lede="Ask about a competitive run or deep research. Answers use only that run’s captured pages and offers, include source links, and call out missing evidence instead of guessing."
        actions={
          <button className="btn-ghost" onClick={startNew}>
            New chat
          </button>
        }
      />

      <div className="chat">
        <aside className="chat-scope">
          <label className="field">
            <span>Scope</span>
            <select value={activeRun} onChange={(e) => { setActiveRun(e.target.value); setActiveSessionId(''); }}>
              <option value="">Most recent run</option>
                {runs.map((run) => (
                  <option key={`${run.kind}-${run.run_id}`} value={run.run_id}>
                    {run.title} · {run.kind === 'deep' ? 'Deep' : 'Competitive'} · {pretty(run.status)}
                </option>
              ))}
            </select>
          </label>
          {current ? (
            <dl className="facts">
              <div><dt>Research path</dt><dd className="mono">{current.kind === 'deep' ? 'Deep research' : 'Competitive run'}</dd></div>
              <div><dt>Sources fetched</dt><dd className="mono">{num(current.kind === 'deep' ? current.coverage?.pages_fetched : current.metrics?.urls_succeeded)}</dd></div>
              <div><dt>{current.kind === 'deep' ? 'Facts extracted' : 'Offers captured'}</dt><dd className="mono">{num(current.kind === 'deep' ? current.coverage?.facts : current.offers_captured)}</dd></div>
            </dl>
          ) : (
            <p className="muted">
              {runs.length ? 'Select a saved run to scope answers to its evidence.' : 'Complete a competitive or deep research run to build an evidence corpus.'}
            </p>
          )}
        </aside>

        <section className="chat-body">
          <div className="chat-log">
            {!messages.length && (
              <div className="chat-intro">
                <h2>Ask about the captured evidence</h2>
                <p>Questions the evidence cannot answer come back as gaps, not guesses.</p>
                <div className="chips">
                  {STARTERS.map((starter) => (
                    <button key={starter} className="chip" onClick={() => setDraft(starter)}>
                      {starter}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {messages.map((message, index) => (
              <article key={index} className={`bubble bubble-${message.role}`}>
                <p>
                  <Inline text={message.content} />
                </p>
                {!!message.citations?.length && (
                  <div className="bubble-sources">
                    {message.citations.map((citation, cIndex) => (
                      <a key={cIndex} href={citation.url} target="_blank" rel="noreferrer">
                        <span className="ref">{citation.ref}</span>
                        {short(citation.heading || citation.title || citation.url, 48)}
                      </a>
                    ))}
                  </div>
                )}
                {message.answerable === false && <span className="tag tag-gap">evidence gap</span>}
              </article>
            ))}

            {busy && (
              <article className="bubble bubble-assistant">
                <p className="muted">Reading the run…</p>
              </article>
            )}
          </div>

          <form className="composer" onSubmit={send}>
            <input
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder={activeRun ? 'Ask a question about this run…' : 'Select a saved research run first…'}
              disabled={busy || !activeRun}
              aria-label="Your question"
            />
            <button className="btn-primary" type="submit" disabled={busy || !draft.trim() || !activeRun} aria-label="Send question">
              <Icon name="arrow" size={16} />
            </button>
          </form>
        </section>
      </div>
    </>
  );
}

/* ------------------------------------------------------------------ */
/* reports                                                             */
/* ------------------------------------------------------------------ */
function Reports() {
  const [reports, setReports] = useState([]);

  useEffect(() => {
    api('/reports')
      .then(setReports)
      .catch(() => {});
  }, []);

  return (
    <>
      <Masthead
        eyebrow="Reports"
        title="Exported documents"
        lede="Generated reports stay linked to their research run so the underlying evidence remains auditable."
      />
      <section className="panel">
        {reports.length ? (
          <ul className="sources">
            {reports.map((report) => {
              const id = report.replace(/^report_|\.pdf$/g, '');
              return (
                <li key={report}>
                  <span className="ref">PDF</span>
                  <a href={`${API}/reports/${id}`} target="_blank" rel="noreferrer">
                    <strong>{id}</strong>
                    <small>Competitive intelligence report</small>
                  </a>
                  <Icon name="external" size={14} />
                </li>
              );
            })}
          </ul>
        ) : (
          <Empty
            title="No reports yet"
            text="Generate a PDF after a completed run with scripts/generate_report.py."
          />
        )}
      </section>
    </>
  );
}

/* ------------------------------------------------------------------ */
/* tiny markdown + inline renderer                                     */
/* ------------------------------------------------------------------ */
function parseMarkdown(markdown) {
  const blocks = [];
  let list = null;

  const flush = () => {
    if (list) {
      blocks.push(list);
      list = null;
    }
  };

  String(markdown || '')
    .split('\n')
    .forEach((raw) => {
      const line = raw.trim();
      if (!line) {
        flush();
        return;
      }
      const heading = line.match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        flush();
        blocks.push({ type: 'h', level: heading[1].length, text: heading[2] });
        return;
      }
      const bullet = line.match(/^[-*+]\s+(.*)$/);
      if (bullet) {
        if (!list) list = { type: 'ul', items: [] };
        list.items.push(bullet[1]);
        return;
      }
      flush();
      blocks.push({ type: 'p', text: line });
    });

  flush();
  return blocks;
}

function Prose({ markdown }) {
  const blocks = useMemo(() => parseMarkdown(markdown), [markdown]);

  return (
    <div className="prose">
      {blocks.map((block, index) => {
        if (block.type === 'h') {
          const Tag = block.level === 1 ? 'h2' : block.level === 2 ? 'h3' : 'h4';
          return <Tag key={index}><Inline text={block.text} /></Tag>;
        }
        if (block.type === 'ul') {
          return (
            <ul key={index}>
              {block.items.map((item, itemIndex) => (
                <li key={itemIndex}><Inline text={item} /></li>
              ))}
            </ul>
          );
        }
        return <p key={index}><Inline text={block.text} /></p>;
      })}
    </div>
  );
}

const INLINE_PATTERN = /(\*\*[^*]+\*\*|`[^`]+`|\[(S\d+)\])/g;

function Inline({ text }) {
  const parts = [];
  const source = String(text || '');
  let cursor = 0;
  let match;

  INLINE_PATTERN.lastIndex = 0;
  while ((match = INLINE_PATTERN.exec(source)) !== null) {
    if (match.index > cursor) parts.push(source.slice(cursor, match.index));
    if (match[2]) {
      parts.push(
        <span className="ref" key={match.index}>
          {match[2]}
        </span>,
      );
    } else if (match[0].startsWith('**')) {
      parts.push(<strong key={match.index}>{match[0].slice(2, -2)}</strong>);
    } else {
      parts.push(<code key={match.index}>{match[0].slice(1, -1)}</code>);
    }
    cursor = match.index + match[0].length;
  }
  if (cursor < source.length) parts.push(source.slice(cursor));

  return parts;
}

createRoot(document.getElementById('root')).render(<App />);
