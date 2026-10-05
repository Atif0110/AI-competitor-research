import React, { useEffect, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './styles.css';

const API = import.meta.env.VITE_API_URL || 'http://localhost:8000';

const NAV = [
  ['dashboard', 'Overview', 'grid'],
  ['run', 'Competitive run', 'target'],
  ['deep', 'Deep research', 'compass'],
  ['evidence', 'Evidence', 'layers'],
  ['signals', 'Signals', 'pulse'],
  ['chat', 'Research chat', 'chat'],
  ['reports', 'Reports', 'doc'],
];

const REGIONS = ['US', 'UK', 'IN', 'EU', 'JP', 'CA', 'AU', 'SG', 'BR'];
const STORAGE_KEY = 'acr_api_key';

// Generous, because a free-tier instance that has spun down takes a while to wake.
const REQUEST_TIMEOUT_MS = 75000;

/* ------------------------------------------------------------------ */
/* data access                                                         */
/* ------------------------------------------------------------------ */
async function api(path, options = {}) {
  const key = localStorage.getItem(STORAGE_KEY) || '';
  const headers = {
    ...(options.body ? { 'Content-Type': 'application/json' } : {}),
    ...(key ? { 'X-API-Key': key } : {}),
    ...(options.headers || {}),
  };

  const method = options.method || 'GET';
  // A Render free instance can take ~50s to wake from idle. Only idempotent
  // reads are retried, so a research job can never be started twice.
  const attempts = method === 'GET' ? 2 : 1;

  let lastError = null;

  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

    let response;
    try {
      response = await fetch(`${API}${path}`, { ...options, headers, signal: controller.signal });
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

    if (response.status === 401) {
      throw new Error(
        'That workspace key was rejected. Open "Workspace key" in the sidebar and paste the API_KEY configured on the API service.',
      );
    }
    if (response.status === 503) {
      throw new Error(
        data?.detail ||
          'The API service refused the request (503). If it says authentication is not configured, set API_KEY in the service environment.',
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
  n == null || n === 0
    ? '—'
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
  const [deepRun, setDeepRun] = useState(null);
  const [navOpen, setNavOpen] = useState(false);

  const loadHealth = () =>
    api('/health')
      .then(setHealth)
      .catch((error) => {
        setHealth({ status: 'error' });
        setToast(error.message);
      });

  useEffect(() => {
    loadHealth();
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

  const openKeyPrompt = () => {
    const entered = window.prompt(
      'Paste the workspace API_KEY configured on the API service (Render → Environment). This is not your model provider key.',
      localStorage.getItem(STORAGE_KEY) || '',
    );
    if (entered === null) return;
    localStorage.setItem(STORAGE_KEY, entered.trim());
    loadHealth();
  };

  const online = health?.status === 'ok';
  const title = NAV.find((entry) => entry[0] === page)?.[1] || 'Overview';

  return (
    <div className="shell">
      <a className="skip" href="#main">
        Skip to content
      </a>

      <nav className={`rail${navOpen ? ' rail-open' : ''}`} aria-label="Main">
        <div className="rail-brand">
          <span className="rail-mark">CR</span>
          <span className="rail-name">
            Competitive<strong>Research</strong>
          </span>
        </div>

        <ul className="rail-list">
          {NAV.map(([id, label, icon]) => (
            <li key={id}>
              <button
                className={page === id ? 'is-active' : ''}
                onClick={() => {
                  setPage(id);
                  setNavOpen(false);
                }}
                aria-current={page === id ? 'page' : undefined}
              >
                <Icon name={icon} size={17} />
                <span>{label}</span>
              </button>
            </li>
          ))}
        </ul>

        <div className="rail-foot">
          <div className="rail-status">
            <span className={`dot${online ? ' dot-live' : ''}`} />
            <div>
              <strong>{online ? health.active_llm_provider : 'API unreachable'}</strong>
              <small>{online ? health.mode : 'check the service'}</small>
            </div>
          </div>
          <button className="rail-key" onClick={openKeyPrompt}>
            <Icon name="key" size={15} />
            Workspace key
          </button>
        </div>
      </nav>

      <main className="main" id="main">
        <header className="topbar">
          <button className="topbar-menu" onClick={() => setNavOpen(true)} aria-label="Open navigation">
            <Icon name="menu" />
          </button>
          <p className="topbar-crumb">{title}</p>
          <div className="topbar-right">
            {health?.llm_provider_chain?.length ? (
              <span className="chain">
                {health.llm_provider_chain.join(' → ')}
              </span>
            ) : null}
            <button className="btn-primary" onClick={() => setPage('deep')}>
              <Icon name="plus" size={15} />
              New research
            </button>
          </div>
        </header>

        <div className="content">
          {health?.status === 'error' && (
            <div className="banner" role="alert">
              <Icon name="alert" size={16} />
              <div>
                <strong>The research API is not responding</strong>
                <p>
                  Every page below needs <span className="mono">{API}</span>. If the service is
                  suspended or still deploying, resume it in the Render dashboard. Nothing on this
                  page can load until it answers.
                </p>
              </div>
              <button className="btn-ghost" onClick={loadHealth}>
                Retry
              </button>
            </div>
          )}

          {page === 'dashboard' && <Dashboard runId={runId} onNavigate={setPage} health={health} />}
          {page === 'run' && <CompetitiveRun onStart={startRun} runId={runId} />}
          {page === 'deep' && (
            <DeepResearch
              result={deepRun}
              onResult={setDeepRun}
              onChat={() => setPage('chat')}
              notify={setToast}
            />
          )}
          {page === 'evidence' && <Evidence runId={runId} notify={setToast} />}
          {page === 'signals' && <Signals runId={runId} />}
          {page === 'chat' && <ResearchChat runId={deepRun?.run_id || null} notify={setToast} />}
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
function Dashboard({ runId, onNavigate, health }) {
  const [metrics, setMetrics] = useState(null);
  const [events, setEvents] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      api('/metrics/aggregate').catch(() => null),
      api(`/insights/events${runId ? `?run_id=${runId}` : ''}`).catch(() => []),
    ])
      .then(([aggregate, activity]) => {
        setMetrics(aggregate);
        setEvents(Array.isArray(activity) ? activity.slice(0, 6) : []);
      })
      .finally(() => setLoading(false));
  }, [runId]);

  const offers = metrics?.offers ?? 0;
  const reviews = metrics?.reviews ?? 0;
  const regions = metrics?.regions ?? 0;

  return (
    <>
      <Masthead
        eyebrow="Overview"
        title="What the evidence says"
        lede="Every number here is traceable to a captured page. Start a run when you have a new target, or interrogate an existing one in research chat."
        actions={
          <>
            <button className="btn-primary" onClick={() => onNavigate('deep')}>
              <Icon name="compass" size={15} />
              Deep research
            </button>
            <button className="btn-ghost" onClick={() => onNavigate('run')}>
              Competitive run
            </button>
          </>
        }
      />

      <section className="stat-row" aria-label="Totals">
        <Stat label="Offers captured" value={num(offers)} note="structured price observations" />
        <Stat label="Reviews indexed" value={num(reviews)} note="searchable review evidence" />
        <Stat label="Regions" value={num(regions)} note="markets observed" />
        <Stat
          label="Routing"
          value={health?.active_llm_provider || '—'}
          note={health?.provider_tiers ? Object.entries(health.provider_tiers).map(([k, v]) => `${k}:${v}`).join(' ') : 'unconfigured'}
          tone="muted"
        />
      </section>

      <div className="split">
        <section className="panel">
          <div className="panel-head">
            <h2>Recent signals</h2>
            <button className="btn-ghost" onClick={() => onNavigate('signals')}>
              All signals
            </button>
          </div>
          {loading ? (
            <div className="skeleton-list" aria-busy="true" aria-label="Loading signals">
              {[0, 1, 2, 3].map((row) => (
                <div className="skeleton-row" key={row}>
                  <span className="skeleton skeleton-dot" />
                  <span className="skeleton skeleton-text" style={{ width: `${52 + row * 9}%` }} />
                </div>
              ))}
            </div>
          ) : events.length ? (
            <ul className="feed">
              {events.map((event, index) => (
                <li key={index}>
                  <span className={`feed-dot feed-${event.severity || 'info'}`} />
                  <div>
                    <strong>{pretty(event.kind)}</strong>
                    <p>{short(event.message, 120)}</p>
                    <small>
                      {event.region || 'cross-region'}
                      {event.competitor ? ` · ${event.competitor}` : ''}
                    </small>
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <Empty
              title="No signals yet"
              text="Signals appear once a research run has captured comparable evidence across runs."
              action="Start a run"
              onAction={() => onNavigate('run')}
            />
          )}
        </section>

        <section className="panel">
          <div className="panel-head">
            <h2>Two ways to research</h2>
          </div>
          <ul className="choice-list">
            <li>
              <button onClick={() => onNavigate('run')}>
                <span className="choice-title">
                  <Icon name="target" size={16} /> Competitive run
                </span>
                <span className="choice-text">
                  A company plus its competitors. Produces structured offers, regional prices and
                  change-over-time insights.
                </span>
              </button>
            </li>
            <li>
              <button onClick={() => onNavigate('deep')}>
                <span className="choice-title">
                  <Icon name="compass" size={16} /> Deep research
                </span>
                <span className="choice-text">
                  One URL, a topic, or a comparison set. Produces a cited report, explicit gaps
                  and a chat corpus.
                </span>
              </button>
            </li>
            <li>
              <button onClick={() => onNavigate('chat')}>
                <span className="choice-title">
                  <Icon name="chat" size={16} /> Research chat
                </span>
                <span className="choice-text">
                  Questions answered only from captured evidence. Says so when it cannot answer.
                </span>
              </button>
            </li>
          </ul>
        </section>
      </div>
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
  const reached = STAGE_ORDER.findIndex((key) => seen.has(key));
  const finished = status === 'completed' || status === 'failed';

  const counts = useMemo(() => {
    const scraped = (events || []).filter((e) => e.event === 'scrape_succeeded').length;
    const extracted = (events || []).filter((e) => e.event === 'extraction_succeeded').length;
    const failed = (events || []).filter((e) => e.event === 'extraction_failed').length;
    return { scraped, extracted, failed };
  }, [events]);

  return (
    <section className="pipeline-viz" aria-label="Research pipeline progress">
      <div className="pipeline-header">
        <h3>Pipeline progress</h3>
        <span className="pipeline-counts">
          {counts.scraped} scraped · {counts.extracted} extracted
          {counts.failed ? ` · ${counts.failed} rejected` : ''}
        </span>
      </div>

      <ol className="pipeline-track">
        {PIPELINE_STAGES.map((stage, index) => {
          const done = seen.has(stage.key);
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
          <li className={`event-row${event.event === 'extraction_failed' ? ' event-bad' : ''}`} key={`${event.timestamp}-${index}`}>
            <span className="event-time mono">{new Date(event.timestamp).toLocaleTimeString()}</span>
            <span className="event-name">{pretty(event.event)}</span>
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
function CompetitiveRun({ onStart, runId }) {
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
        if (!cancelled) setLoadError(error.message);
      });
    return () => {
      cancelled = true;
    };
  }, [runId]);

  // Poll only while the job is actually in flight.
  useEffect(() => {
    if (!job || ['completed', 'failed'].includes(job.status)) return undefined;
    const timer = setInterval(() => {
      api(`/research/jobs/${job.job_id}`)
        .then((data) => {
          setJob(data);
          setLoadError('');
        })
        .catch((error) => setLoadError(error.message));
    }, 2000);
    return () => clearInterval(timer);
  }, [job]);

  // A visible clock, so a slow run never looks like a dead one.
  useEffect(() => {
    if (!job || ['completed', 'failed'].includes(job.status)) return undefined;
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
    const competitorsData = competitorNames.map((name, i) => ({
      name,
      website: competitorUrlsList[i] || `https://${name.toLowerCase().replace(/\s+/g, '')}.com`,
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
              <span>Focus products</span>
              <input value={products} onChange={(e) => setProducts(e.target.value)} placeholder="Pro X Headphones, Air Buds Lite" />
              <small>Comma separated. Leave empty to discover products automatically.</small>
            </label>
            <label className="field">
              <span>Competitors</span>
              <input value={competitors} onChange={(e) => setCompetitors(e.target.value)} placeholder="Obsidian, Coda, Roam Research" />
              <small>Comma separated names.</small>
            </label>
            <label className="field">
              <span>Competitor websites</span>
              <input value={competitorUrls} onChange={(e) => setCompetitorUrls(e.target.value)} placeholder="https://obsidian.md, https://coda.io, https://roamresearch.com" />
              <small>Comma separated URLs (matching order of names above). Optional — we'll guess if omitted.</small>
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
              {job && ['completed', 'failed'].includes(job.status) && (
                <button
                  className="btn-ghost"
                  onClick={() => api(`/research/jobs/${runId}`).then(setJob).catch((e) => setLoadError(e.message))}
                >
                  <Icon name="refresh" size={14} />
                  Refresh
                </button>
              )}
            </div>
          </div>

          {loadError && <p className="error-text">{loadError}</p>}

          {job ? (
            <>
              <ResearchPipeline status={job.status} events={job.events} />
              <div className="run-state">
                <Badge tone={job.status === 'failed' ? 'bad' : job.status === 'completed' ? 'good' : 'busy'}>
                  {job.status}
                </Badge>
                {!['completed', 'failed'].includes(job.status) && (
                  <span className="mono run-clock">{elapsed}s elapsed</span>
                )}
                <p className="muted">
                  {job.current_event?.event
                    ? `${pretty(job.current_event.event)}${job.current_event.url ? ` · ${job.current_event.url}` : ''}`
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
                    {job.result.offers != null && (
                      <div><dt>Offers captured</dt><dd className="mono">{num(job.result.offers)}</dd></div>
                    )}
                    {job.result.runs != null && (
                      <div><dt>Runs recorded</dt><dd className="mono">{num(job.result.runs)}</dd></div>
                    )}
                    {job.result.reviews != null && (
                      <div><dt>Reviews stored</dt><dd className="mono">{num(job.result.reviews)}</dd></div>
                    )}
                  </dl>
                )}
              </div>
            </>
          ) : runId ? (
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

function DeepResearch({ result, onResult, onChat, notify }) {
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
      notify(error.message);
    } finally {
      setBusy(false);
    }
  };

  const coverage = result?.coverage;

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
            <button className="btn-primary" disabled={busy}>
              {busy ? 'Researching…' : 'Start deep research'}
            </button>
            <small className="muted">Runs synchronously. Larger page budgets take longer.</small>
          </div>
        </form>
      </section>

      {result && (
        <>
          <section className="stat-row" aria-label="Coverage">
            <Stat label="Status" value={pretty(result.status)} note={`${result.duration_s}s`} />
            <Stat label="Pages read" value={num(coverage?.pages_fetched)} note={`${num(coverage?.pages_failed)} failed`} />
            <Stat label="Sections" value={num(coverage?.sections)} note={`${num(coverage?.facts)} facts extracted`} />
            <Stat label="Evidence" value={num(coverage?.characters)} unit=" chars" note={coverage?.search_backend ? `search: ${coverage.search_backend}` : 'no web search'} />
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
  const [region, setRegion] = useState('');
  const [product, setProduct] = useState('');
  const [selected, setSelected] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    const params = new URLSearchParams({ limit: '200' });
    if (region) params.set('region', region);
    if (product) params.set('product', product);
    if (runId) params.set('run_id', runId);

    api(`/offers?${params}`)
      .then(setRows)
      .catch((error) => notify(error.message))
      .finally(() => setLoading(false));
  }, [region, product, runId]);

  return (
    <>
      <Masthead
        eyebrow="Evidence"
        title="Captured observations"
        lede="Each row keeps its source URL, capture time and extraction confidence so any claim can be checked."
      />

      <section className="panel">
        <div className="filters">
          <label className="field field-inline">
            <span>Product</span>
            <input value={product} onChange={(e) => setProduct(e.target.value)} placeholder="Filter by product" />
          </label>
          <label className="field field-inline">
            <span>Region</span>
            <select value={region} onChange={(e) => setRegion(e.target.value)}>
              <option value="">All regions</option>
              {REGIONS.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </label>
        </div>

        {loading ? (
          <div className="skeleton-table" aria-busy="true" aria-label="Loading observations">
            {[0, 1, 2, 3, 4, 5].map((row) => (
              <div className="skeleton-row" key={row}>
                <span className="skeleton skeleton-cell" style={{ width: '22%' }} />
                <span className="skeleton skeleton-cell" style={{ width: '12%' }} />
                <span className="skeleton skeleton-cell" style={{ width: '10%' }} />
                <span className="skeleton skeleton-cell" style={{ width: '16%' }} />
              </div>
            ))}
          </div>
        ) : rows.length ? (
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Product</th>
                <th scope="col">Price</th>
                <th scope="col">Region</th>
                <th scope="col">Availability</th>
                <th scope="col" className="align-right">Confidence</th>
                <th scope="col"><span className="sr-only">Open</span></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((offer, index) => (
                <tr key={index} onClick={() => setSelected(offer)} className="clickable">
                  <td>{offer.product_name}</td>
                  <td className="mono">{money(offer.price, offer.currency)}</td>
                  <td>{offer.region}</td>
                  <td>{pretty(offer.availability)}</td>
                  <td className="align-right mono">
                    {offer.extraction_confidence != null
                      ? `${Math.round(offer.extraction_confidence * 100)}%`
                      : '—'}
                  </td>
                  <td className="align-right">
                    <Icon name="arrow" size={14} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty
            title="No observations yet"
            text="Run a competitive research run to capture structured price observations."
          />
        )}
      </section>

      {selected && (
        <div className="scrim" onClick={() => setSelected(null)} role="presentation">
          <aside className="drawer" onClick={(e) => e.stopPropagation()} aria-label="Observation detail">
            <div className="drawer-head">
              <h2>{selected.product_name}</h2>
              <button className="btn-ghost" onClick={() => setSelected(null)} aria-label="Close">
                ×
              </button>
            </div>
            <p className="drawer-price mono">{money(selected.price, selected.currency)}</p>
            <dl className="facts">
              <div><dt>Region</dt><dd>{selected.region}</dd></div>
              <div><dt>Availability</dt><dd>{pretty(selected.availability)}</dd></div>
              <div><dt>Competitor</dt><dd>{selected.competitor || '—'}</dd></div>
              <div><dt>Captured</dt><dd>{selected.scraped_at ? new Date(selected.scraped_at).toLocaleString() : '—'}</dd></div>
            </dl>
            <div className="drawer-source">
              <span className="eyebrow">Source</span>
              <a href={selected.url} target="_blank" rel="noreferrer">
                {short(selected.url, 70)}
                <Icon name="external" size={13} />
              </a>
            </div>
          </aside>
        </div>
      )}
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

function ResearchChat({ runId, notify }) {
  const [runs, setRuns] = useState([]);
  const [activeRun, setActiveRun] = useState(runId || '');
  const [sessionId, setSessionId] = useState(() => localStorage.getItem('acr_chat_session') || '');
  const [messages, setMessages] = useState([]);
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api('/research/deep/runs')
      .then(setRuns)
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (runId) setActiveRun(runId);
  }, [runId]);

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
  }, [sessionId]);

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
      if (answer.session_id && answer.session_id !== sessionId) {
        localStorage.setItem('acr_chat_session', answer.session_id);
        setSessionId(answer.session_id);
      }
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
    localStorage.removeItem('acr_chat_session');
    setSessionId('');
    setMessages([]);
  };

  const current = runs.find((run) => run.run_id === activeRun);

  return (
    <>
      <Masthead
        eyebrow="Research chat"
        title="Interrogate the evidence"
        lede="Answers are built only from sections the selected run captured. When the evidence does not cover a question, that is reported rather than guessed."
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
            <select value={activeRun} onChange={(e) => setActiveRun(e.target.value)}>
              <option value="">Most recent run</option>
              {runs.map((run) => (
                <option key={run.run_id} value={run.run_id}>
                  {run.run_id} · {pretty(run.status)}
                </option>
              ))}
            </select>
          </label>
          {current ? (
            <dl className="facts">
              <div><dt>Pages</dt><dd className="mono">{num(current.coverage?.pages_fetched)}</dd></div>
              <div><dt>Sections</dt><dd className="mono">{num(current.coverage?.sections)}</dd></div>
              <div><dt>Facts</dt><dd className="mono">{num(current.coverage?.facts)}</dd></div>
            </dl>
          ) : (
            <p className="muted">
              {runs.length ? 'Using the most recent run.' : 'Run deep research first to build a corpus.'}
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
              placeholder={runs.length ? 'Ask a question about this run…' : 'Run deep research first…'}
              disabled={busy}
              aria-label="Your question"
            />
            <button className="btn-primary" type="submit" disabled={busy || !draft.trim()} aria-label="Send question">
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