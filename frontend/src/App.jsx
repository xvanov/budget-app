import React, { useState, useEffect, useMemo, useCallback, useRef, useContext } from 'react'
import { createPortal } from 'react-dom'
import SankeyPanel from './SankeyPanel.jsx'

// Context for shared state.
// dataVersion is a monotonically-increasing counter; pages that display
// transaction-derived data include it in their fetch effect deps so they
// re-fetch after a chat-correct mutation. bumpDataVersion() is called by
// the chat-correct UI on success.
export const AppContext = React.createContext({
  dataVersion: 0,
  bumpDataVersion: () => {},
  categoryColors: {},
  palette: [],
  reloadColors: () => {},
})

// ───── API helpers ─────
const API_BASE = '/api'

// `opts` is forwarded to fetch. It used to be silently dropped, which broke every
// non-GET caller in a way that looked like a server bug: deleting or renaming a
// category called this with {method:'DELETE'|'PUT'}, the method never left the
// browser, and the resulting GET on /api/categories/{id} — a path with no GET
// route — came back as "API error: 405".
async function apiFetch(path, opts) {
  const res = await fetch(`${API_BASE}${path}`, opts)
  if (!res.ok) {
    const detail = await res.json().then(j => j?.detail).catch(() => null)
    throw new Error(typeof detail === 'string' ? detail : `API error: ${res.status}`)
  }
  // 204 and other empty bodies are a legitimate success for DELETE.
  if (res.status === 204) return {}
  return res.json().catch(() => ({}))
}

async function apiPost(path, body) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    // FastAPI puts the useful sentence in `detail`. Surfacing "API error: 400"
    // instead throws away the only thing that tells the user what to fix.
    const detail = await res.json().then(j => j?.detail).catch(() => null)
    throw new Error(typeof detail === 'string' ? detail : `API error: ${res.status}`)
  }
  return res.json()
}

function fmtCurrency(cents, opts = {}) {
  if (cents == null) return '$0'
  const dollars = Math.abs(cents) / 100
  // `full` opt forces full precision (no K/M abbreviations) with cents shown.
  // Used by the transaction table where exact amounts matter; dashboard KPI
  // cards and charts keep the compact `$358k` / `$1.5M` defaults.
  if (opts.full) {
    const d = opts.decimals != null ? opts.decimals : 2
    const str = '$' + dollars.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d })
    return cents < 0 ? '−' + str : str
  }
  const decimals = opts.decimals != null ? opts.decimals : (dollars >= 1000 ? 0 : 2)
  let str
  if (dollars >= 1000000) str = '$' + (dollars / 1000000).toFixed(1) + 'M'
  else if (dollars >= 10000) str = '$' + (dollars / 1000).toFixed(0) + 'k'
  else str = '$' + dollars.toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals })
  return cents < 0 ? '−' + str : str
}

// Explicit sign on both directions, without double-printing the minus:
// fmtCurrency already renders negatives as "−$16.29", so prefixing another "−"
// produced "--$16.29" everywhere a transaction amount was shown.
function fmtSignedCurrency(cents) {
  const full = fmtCurrency(cents, { full: true })
  return cents >= 0 ? '+' + full : full
}

function fmtPct(n, decimals = 1) {
  return (n >= 0 ? '+' : '') + n.toFixed(decimals) + '%'
}

// ───── Period helpers ─────
function startOfMonth(d) { return new Date(d.getFullYear(), d.getMonth(), 1) }
function endOfMonth(d) { return new Date(d.getFullYear(), d.getMonth() + 1, 0, 23, 59, 59) }
function addMonths(d, n) { return new Date(d.getFullYear(), d.getMonth() + n, d.getDate()) }
function fmtDate(d) { return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) }
function fmtDateLong(d) { return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }) }
function fmtMonth(d) { return d.toLocaleDateString('en-US', { month: 'short', year: 'numeric' }) }
function fmtDateParam(d) { const y = d.getFullYear(); const m = String(d.getMonth() + 1).padStart(2, '0'); const day = String(d.getDate()).padStart(2, '0'); return `${y}-${m}-${day}` }

const PERIOD_PRESETS = [
  { id: 'this-month', label: 'This Month' },
  { id: 'last-month', label: 'Last Month' },
  { id: 'last-3', label: 'Last 3 Months' },
  { id: 'last-6', label: 'Last 6 Months' },
  { id: 'ytd', label: 'Year to Date' },
  { id: 'last-12', label: 'Last 12 Months' },
  { id: 'all', label: 'All Time' },
]

const ROUTE_ALIASES = { '/accounts.html': '/accounts' }

// Routes whose content is scoped by the global period. Every other route hides
// the topbar control rather than showing a dial that changes nothing.
const PERIOD_ROUTES = new Set([
  '/', '/txns', '/income', '/expenses', '/refunded', '/excluded', '/budgets', '/trends',
])

function resolvePeriod(id) {
  const now = new Date()
  switch (id) {
    case 'this-month': return { start: startOfMonth(now), end: now, label: fmtMonth(now) }
    case 'last-month': { const s = startOfMonth(addMonths(now, -1)); return { start: s, end: endOfMonth(s), label: fmtMonth(s) } }
    case 'last-3': return { start: startOfMonth(addMonths(now, -2)), end: now, label: 'Last 3 mo' }
    case 'last-6': return { start: startOfMonth(addMonths(now, -5)), end: now, label: 'Last 6 mo' }
    case 'ytd': return { start: new Date(now.getFullYear(), 0, 1), end: now, label: now.getFullYear() + ' YTD' }
    case 'last-12': return { start: startOfMonth(addMonths(now, -11)), end: now, label: 'Last 12 mo' }
    case 'all': return { start: new Date(2020, 0, 1), end: now, label: 'All time' }
    default: return resolvePeriod('this-month')
  }
}

// ───── Sparkline ─────
function Sparkline({ values, color = 'var(--primary)', height = 28, fill = true }) {
  const pad = 2
  const w = 100, h = height
  // A one-month period gives a one-point series: i/(length-1) is 0/0, which used
  // to emit `d="MNaN,26"` and an SVG error per card. Nothing to trend, so draw
  // nothing; two points is the minimum a line can be.
  if (!Array.isArray(values) || values.length < 2) return null
  const max = Math.max(...values), min = Math.min(...values)
  const range = max - min || 1
  const pts = values.map((v, i) => {
    const x = pad + (i / (values.length - 1)) * (w - pad * 2)
    const y = pad + (1 - (v - min) / range) * (h - pad * 2)
    return [x, y]
  })
  const d = pts.map((p, i) => (i === 0 ? 'M' : 'L') + p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ')
  const d2 = d + ` L${pts.at(-1)[0]},${h} L${pts[0][0]},${h} Z`
  return (
    <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" className="kpi-spark">
      {fill && <path d={d2} fill={color} opacity={0.2}/>}
      <path d={d} fill="none" stroke={color} strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round"/>
    </svg>
  )
}

// ───── Period control ─────
// One instance, in the topbar, shared by every period-aware route (see
// PERIOD_ROUTES). Seven segments plus the resolved range need ~830px of header;
// below 1280px viewport that no longer fits next to the title, so the same
// presets render as a dropdown instead of overflowing or pushing the title out.
const PERIOD_SEG_MIN = 1280

function useMediaQuery(query) {
  const [matches, setMatches] = useState(() =>
    typeof window !== 'undefined' && window.matchMedia(query).matches)
  useEffect(() => {
    const mq = window.matchMedia(query)
    const onChange = e => setMatches(e.matches)
    setMatches(mq.matches)
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [query])
  return matches
}

function PeriodControl({ value, onChange }) {
  const period = resolvePeriod(value)
  const roomForSegments = useMediaQuery(`(min-width: ${PERIOD_SEG_MIN}px)`)
  const [open, setOpen] = useState(false)
  const ref = useRef(null)

  useEffect(() => {
    if (!open) return
    const onDown = e => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    const onKey = e => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const range = (
    <div className="period-range">
      <span className="mono">{fmtDate(period.start)}</span> → <span className="mono">{fmtDate(period.end)}</span>
    </div>
  )

  if (!roomForSegments) {
    const active = PERIOD_PRESETS.find(p => p.id === value)
    return (
      <div className="topbar-period is-compact" ref={ref} data-testid="period-control">
        <button className="btn btn--outline btn--sm" aria-expanded={open}
                onClick={() => setOpen(o => !o)}>
          <Icon name="Calendar" size={14}/> {active?.label || 'Period'} <Icon name="ChevronDown" size={14}/>
        </button>
        {open && (
          <div className="popover period-popover">
            {PERIOD_PRESETS.map(p => (
              <div key={p.id} className={`popover-item ${value === p.id ? 'is-active' : ''}`}
                   onClick={() => { onChange(p.id); setOpen(false) }}>
                {value === p.id ? <Icon name="Check" size={14}/> : <span style={{width:14}}/>}
                {p.label}
              </div>
            ))}
            <div className="popover-sep"/>
            <div style={{padding:'2px 10px 4px'}}>{range}</div>
          </div>
        )}
      </div>
    )
  }

  return (
    <div className="topbar-period" data-testid="period-control">
      <Icon name="Calendar" size={14}/>
      <div className="seg">
        {PERIOD_PRESETS.map(p => (
          <button key={p.id} onClick={() => onChange(p.id)} className={`seg-item ${value === p.id ? 'is-active' : ''}`}>{p.label}</button>
        ))}
      </div>
      {range}
    </div>
  )
}

// ───── Icons ─────
function Icon({ name, size = 16 }) {
  const s = { width: size, height: size, flexShrink: 0, display: 'inline-block', verticalAlign: 'middle' }
  const svgProps = { width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 2, strokeLinecap: 'round', strokeLinejoin: 'round', style: s }

  switch (name) {
    case 'LayoutDashboard': return <svg {...svgProps}><rect width="7" height="9" x="3" y="3" rx="1"/><rect width="7" height="5" x="14" y="3" rx="1"/><rect width="7" height="9" x="14" y="12" rx="1"/><rect width="7" height="5" x="3" y="16" rx="1"/></svg>
    case 'Receipt': return <svg {...svgProps}><path d="M4 2v20l2-1 2 1 2-1 2 1 2-1 2 1 2-1 2 1V2l-2 1-2-1-2 1-2-1-2 1-2-1-2 1-2-1Z"/><path d="M16 8h-6a2 2 0 1 0 0 4h4a2 2 0 1 1 0 4H8"/><path d="M12 17.5v-11"/></svg>
    case 'Wallet': return <svg {...svgProps}><path d="M21 12V7H5a2 2 0 0 1 0-4h14v4"/><path d="M3 5v14a2 2 0 0 0 2 2h16v-5"/><path d="M18 12a2 2 0 0 0 0 4h4v-4Z"/></svg>
    case 'GitMerge': return <svg {...svgProps}><circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><path d="M6 21V9a9 9 0 0 0 9 9"/></svg>
    case 'TrendingUp': return <svg {...svgProps}><polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/></svg>
    case 'Target': return <svg {...svgProps}><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/></svg>
    case 'ChevronRight': return <svg {...svgProps}><polyline points="9 18 15 12 9 6"/></svg>
    case 'Search': return <svg {...svgProps}><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
    case 'Activity': return <svg {...svgProps}><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
    case 'Settings': return <svg {...svgProps}><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9"/></svg>
    case 'Building': return <svg {...svgProps}><rect width="16" height="20" x="4" y="2" rx="2" ry="2"/><path d="M9 22v-4h6v4"/><path d="M8 6h.01M16 6h.01M12 6h.01M12 10h.01M12 14h.01M16 10h.01M16 14h.01M8 10h.01M8 14h.01"/></svg>
    case 'Briefcase': return <svg {...svgProps}><rect width="20" height="14" x="2" y="7" rx="2" ry="2"/><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/></svg>
    case 'Tag': return <svg {...svgProps}><path d="M20.59 13.41 13.42 20.58a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/></svg>
    case 'Upload': return <svg {...svgProps}><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
    case 'Filter': return <svg {...svgProps}><polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/></svg>
    case 'X': return <svg {...svgProps}><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
    case 'FileText': return <svg {...svgProps}><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg>
    case 'PieChart': return <svg {...svgProps}><path d="M21.21 15.89A10 10 0 1 1 8 2.83"/><path d="M22 12A10 10 0 0 0 12 2v10z"/></svg>
    case 'Plus': return <svg {...svgProps}><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
    case 'HelpCircle': return <svg {...svgProps}><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
    case 'AlertTriangle': return <svg {...svgProps}><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
    case 'Lightbulb': return <svg {...svgProps}><path d="M9 18h6"/><path d="M10 22h4"/><path d="M12 2a7 7 0 0 0-4 12.74V17a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1v-2.26A7 7 0 0 0 12 2z"/></svg>
    case 'Repeat': return <svg {...svgProps}><polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/></svg>
    case 'CircleAlert': return <svg {...svgProps}><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
    case 'Sparkles': return <svg {...svgProps}><path d="M12 3l1.91 5.18L19 10l-5.09 1.82L12 17l-1.91-5.18L5 10l5.09-1.82z"/></svg>
    case 'MessageSquare': return <svg {...svgProps}><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
    case 'ChevronDown': return <svg {...svgProps}><path d="m6 9 6 6 6-6"/></svg>
    case 'Calendar': return <svg {...svgProps}><rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>
    case 'PanelLeft': return <svg {...svgProps}><rect width="18" height="18" x="3" y="3" rx="2"/><path d="M9 3v18"/></svg>
    case 'Trash': return <svg {...svgProps}><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M10 11v6M14 11v6"/></svg>
    case 'Check': return <svg {...svgProps}><path d="M20 6 9 17l-5-5"/></svg>
    // Spinner: the arc is drawn open on purpose — a full circle gives no visual
    // cue that it is rotating. Callers spin it with the `spin` class.
    case 'Loader': return <svg {...svgProps} className="spin"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg>
    default: return <svg {...svgProps}><circle cx="12" cy="12" r="3"/></svg>
  }
}

// ───── KPI Card ─────
function KpiCard({ label, value, sub, delta, sparkValues, color = 'var(--primary)', valueClass = '' }) {
  let deltaStr = null
  if (delta != null && isFinite(delta)) {
    const abs = Math.abs(delta)
    if (abs > 999) deltaStr = (delta >= 0 ? '+' : '−') + '>999%'
    else deltaStr = fmtPct(delta, abs >= 100 ? 0 : 1)
  }
  return (
    <div className="card kpi">
      <div className="kpi-label">{label}</div>
      <div className={'kpi-value ' + valueClass}>{value}</div>
      {sub && <div className="kpi-delta" style={{ whiteSpace: 'nowrap' }}>{sub}</div>}
      {deltaStr != null && (
        <div className="kpi-delta" style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
          <span className={delta >= 0 ? 'up' : 'down'}>{delta >= 0 ? '▲' : '▼'} {deltaStr}</span>
        </div>
      )}
      {sparkValues && <Sparkline values={sparkValues} color={color} />}
    </div>
  )
}

// ───── Dashboard Page ─────
function Dashboard({ periodId, onNavigate }) {
  const { dataVersion } = useContext(AppContext)
  const [currentData, setCurrentData] = useState(null)
  const [prevData, setPrevData] = useState(null)
  const [accts, setAccts] = useState([])
  const [recentTxns, setRecentTxns] = useState([])
  const [sankeyData, setSankeyData] = useState(null)
  const [drillCategory, setDrillCategory] = useState(null)
  const [drillData, setDrillData] = useState(null)
  const [drillLoading, setDrillLoading] = useState(false)
  const [drillPage, setDrillPage] = useState(1)
  const [drillSort, setDrillSort] = useState('posted_date')
  const [drillDir, setDrillDir] = useState('desc')
  const [loading, setLoading] = useState(true)

  const period = useMemo(() => resolvePeriod(periodId), [periodId])

  async function handleDrillCategory(catPath) {
    setDrillCategory(catPath)
    setDrillLoading(true)
    setDrillPage(1)
    setDrillSort('posted_date')
    setDrillDir('desc')
    try {
      const ps = fmtDateParam(period.start)
      const pe = fmtDateParam(period.end)
      const data = await apiFetch(`/dashboard/category-detail?path=${encodeURIComponent(catPath)}&start_date=${ps}&end_date=${pe}&page_size=100`)
      setDrillData(data)
    } catch (e) {
      setDrillData(null)
    } finally {
      setDrillLoading(false)
    }
  }

  async function handleDrillSort(col) {
    if (!drillCategory) return
    const newDir = drillSort === col && drillDir === 'asc' ? 'desc' : 'asc'
    setDrillSort(col)
    setDrillDir(newDir)
    setDrillLoading(true)
    try {
      const ps = fmtDateParam(period.start)
      const pe = fmtDateParam(period.end)
      const data = await apiFetch(`/dashboard/category-detail?path=${encodeURIComponent(drillCategory)}&start_date=${ps}&end_date=${pe}&page_size=100&sort=${col}&dir=${newDir}`)
      setDrillData(data)
    } catch (e) {
      setDrillData(null)
    } finally {
      setDrillLoading(false)
    }
  }

  useEffect(() => {
    setLoading(true)
    const p = resolvePeriod(periodId)
    const now = new Date()

    let prevStart, prevEnd
    if (periodId === 'this-month') {
      const s = startOfMonth(addMonths(now, -1))
      prevStart = s
      prevEnd = new Date(s.getFullYear(), s.getMonth(), now.getDate(), 23, 59, 59)
    } else if (periodId === 'last-month') {
      const s = startOfMonth(addMonths(now, -2))
      prevStart = s
      prevEnd = endOfMonth(s)
    } else if (periodId === 'ytd') {
      prevStart = new Date(now.getFullYear() - 1, 0, 1)
      prevEnd = new Date(now.getFullYear() - 1, now.getMonth(), now.getDate(), 23, 59, 59)
    } else {
      const span = p.end - p.start
      prevStart = new Date(p.start.getTime() - span - 1)
      prevEnd = new Date(p.start.getTime() - 1)
    }

    const ps = fmtDateParam(p.start)
    const pe = fmtDateParam(p.end)
    const pps = fmtDateParam(prevStart)
    const ppe = fmtDateParam(prevEnd)

    Promise.all([
      apiFetch(`/dashboard?start_date=${ps}&end_date=${pe}`),
      apiFetch(`/dashboard?start_date=${pps}&end_date=${ppe}`),
      apiFetch('/accounts'),
      apiFetch('/transactions?page_size=12'),
      apiFetch(`/sankey?start_date=${ps}&end_date=${pe}`),
    ]).then(([cur, prev, acctsData, txnsData, sankey]) => {
      setCurrentData(cur)
      setPrevData(prev)
      setAccts(Array.isArray(acctsData) ? acctsData : [])
      setRecentTxns(txnsData?.transactions || [])
      setSankeyData(sankey)
      setLoading(false)
    }).catch(() => setLoading(false))
  }, [periodId, dataVersion])

  if (loading) {
    return (
      <div className="page">
        <div className="subtitle">Loading data...</div>
      </div>
    )
  }

  const totals = currentData?.totals || { total_income_cents: 0, total_expense_cents: 0, net_cents: 0 }
  const prevTotals = prevData?.totals || { total_income_cents: 0, total_expense_cents: 0, net_cents: 0 }
  const monthly = currentData?.monthly || []
  const byCategory = currentData?.by_category || []
  const txnCount = currentData?.transaction_count || 0
  const acctCount = accts.length

  const pctDelta = (cur, prev) => prev === 0 ? 0 : ((cur - prev) / prev) * 100

  return (
    <div className="page">
      <div className="page-intro">
        <div className="subtitle">{period.label} · {txnCount} transactions across {acctCount} accounts</div>
      </div>

      <div className="kpi-grid">
        <KpiCard label="Net Cash Flow"
                 value={fmtCurrency(totals.net_cents)}
                 valueClass={totals.net_cents >= 0 ? "pos" : "neg"}
                 delta={pctDelta(totals.net_cents, prevTotals.net_cents)}
                 deltaLabel="vs prev period"
                 sparkValues={monthly.map(m => m.income_cents - m.expense_cents)}
                 color={totals.net_cents >= 0 ? "#4ade80" : "var(--destructive)"}/>
        <KpiCard label="Income"
                 value={fmtCurrency(totals.total_income_cents)}
                 delta={pctDelta(totals.total_income_cents, prevTotals.total_income_cents)}
                 deltaLabel="vs prev period"
                 sparkValues={monthly.map(m => m.income_cents)}
                 color="#22c55e"/>
        <KpiCard label="Expenses"
                 value={fmtCurrency(totals.total_expense_cents)}
                 delta={pctDelta(totals.total_expense_cents, prevTotals.total_expense_cents)}
                 deltaLabel="vs prev period"
                 sparkValues={monthly.map(m => m.expense_cents)}
                 color="#f43f5e"/>
        <KpiCard label="Savings Rate"
                 value={totals.total_income_cents > 0 ? ((totals.net_cents / totals.total_income_cents) * 100).toFixed(1) + '%' : '0%'}
                 sub={<span style={{color:"var(--muted-foreground)"}}><Icon name="Target" size={12}/> Goal 35%</span>}
                 sparkValues={monthly.map(m => m.income_cents > 0 ? ((m.income_cents - m.expense_cents) / m.income_cents) * 100 : 0)}
                 color="#a78bfa"/>
      </div>

      {/* Sankey Cash Flow */}
      <div className="card">
        <div className="section-head">
          <div>
            <div className="h">Cash Flow</div>
            <div className="sub">Where every dollar earned in {period.label} ended up. Hover any node or link to highlight its path.</div>
          </div>
          <div className="filter-row">
            <span className="label">Total Income</span>
            <span className="num" style={{fontWeight:600}}>{fmtCurrency(totals.total_income_cents)}</span>
            <span style={{margin:"0 6px",opacity:0.5}}>·</span>
            <span className="label">Net</span>
            <span className="num" style={{fontWeight:600, color: totals.net_cents >= 0 ? "#4ade80" : "var(--destructive)"}}>{fmtCurrency(totals.net_cents)}</span>
          </div>
        </div>
        <SankeyPanel data={sankeyData} />
      </div>

      {/* Recent Transactions + Top Spending */}
      <div style={{display:"grid", gridTemplateColumns:"2fr 1fr", gap:16}}>
        <div className="card">
          <div className="section-head">
            <div>
              <div className="h">Recent Transactions</div>
              <div className="sub">{recentTxns.length} most recent transactions in {period.label}</div>
            </div>
            <button className="btn btn--ghost btn--sm">View all <Icon name="ChevronRight" size={12}/></button>
          </div>
          <TxnTable txns={recentTxns.slice(0, 12)} compact />
        </div>
        <TopSpendCard categories={byCategory} period={period} onDrill={handleDrillCategory} />
      </div>

      {drillCategory && drillData && (
        <div className="modal-overlay" onClick={() => setDrillCategory(null)}>
          <div className="modal" onClick={e => e.stopPropagation()} style={{maxWidth:800, maxHeight:'80vh', overflow:'auto'}}>
            <div className="modal-header">
              <div className="modal-title">{drillCategory}</div>
              <button className="btn btn--ghost btn--sm" onClick={() => setDrillCategory(null)}><Icon name="X" size={14}/></button>
            </div>
            <div className="modal-body" style={{maxHeight:'60vh', overflow:'auto'}}>
              {drillLoading ? (
                <div className="sub" style={{textAlign:'center', padding:24}}>Loading...</div>
              ) : (
                <>
                  <div className="sub" style={{marginBottom:12}}>{drillData.total} transactions in {drillCategory}</div>
                  <table className="tbl">
                    <thead>
                      <tr>
                        <th style={{cursor:'pointer', userSelect:'none'}} onClick={() => handleDrillSort('posted_date')}>Date{drillSort === 'posted_date' ? (drillDir === 'asc' ? ' ▲' : ' ▼') : ''}</th>
                        <th>Merchant</th>
                        <th style={{cursor:'pointer', userSelect:'none'}} onClick={() => handleDrillSort('amount_cents')}>Amount{drillSort === 'amount_cents' ? (drillDir === 'asc' ? ' ▲' : ' ▼') : ''}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {drillData.transactions.map(t => (
                        <tr key={t.id}>
                          <td className="mono" style={{fontSize:12}}>{t.posted_date}</td>
                          <td>
                            <div style={{fontWeight:500}}>{t.merchant}</div>
                            <div style={{fontSize:11, color:'var(--muted-foreground)'}}>{t.description}</div>
                          </td>
                          <td className={`text-right mono ${t.amount_cents >= 0 ? 'amt-pos' : 'amt-neg'}`}>{fmtCurrency(t.amount_cents, {full:true})}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <div style={{textAlign:'center', marginTop:12}}>
                    <button className="btn btn--outline btn--sm" onClick={() => onNavigate && onNavigate(`/txns?category=${encodeURIComponent(drillCategory)}`)}>
                      View in transactions page
                    </button>
                  </div>
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}


// Category type-ahead. Replaces a raw window.prompt() that required typing the
// full path by hand, '>' separators and all — a category had to be spelled
// exactly right or the edit silently did nothing useful.
function CategoryPicker({ value, options, onPick, onCancel }) {
  const [query, setQuery] = useState('')
  const [highlight, setHighlight] = useState(0)
  const inputRef = useRef(null)
  const wrapRef = useRef(null)
  const menuRef = useRef(null)
  // The transactions table lives inside `.card { overflow: auto }`, which CLIPS an
  // absolutely-positioned dropdown. The list was rendering the whole time — it was
  // just cut off outside the scroll box, which is why editing a category inline
  // looked like it did nothing. Portal the menu to <body> and pin it with fixed
  // coordinates measured from the input.
  const [anchor, setAnchor] = useState(null)

  useEffect(() => { inputRef.current?.focus() }, [])

  useEffect(() => {
    function measure() {
      const el = wrapRef.current
      if (!el) return
      const r = el.getBoundingClientRect()
      setAnchor({ left: r.left, top: r.top, bottom: r.bottom, width: r.width })
    }
    measure()
    // Capture-phase scroll: the ancestor that moves is the table's scroll box,
    // not the window, and scroll does not bubble.
    window.addEventListener('scroll', measure, true)
    window.addEventListener('resize', measure)
    return () => {
      window.removeEventListener('scroll', measure, true)
      window.removeEventListener('resize', measure)
    }
  }, [])

  // Click-away closes the picker. Without this an open picker survived clicking
  // elsewhere, so two rows could look editable at once. Deferred by a tick so the
  // very mousedown that opened this picker cannot immediately close it. onCancel
  // is held in a ref because callers pass an inline arrow — a dep on it would
  // tear down and re-arm the listener on every keystroke.
  const cancelRef = useRef(onCancel)
  cancelRef.current = onCancel
  useEffect(() => {
    let handler = null
    const id = setTimeout(() => {
      handler = (e) => {
        if (wrapRef.current?.contains(e.target)) return
        if (menuRef.current?.contains(e.target)) return
        cancelRef.current?.()
      }
      document.addEventListener('mousedown', handler, true)
    }, 0)
    return () => {
      clearTimeout(id)
      if (handler) document.removeEventListener('mousedown', handler, true)
    }
  }, [])

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return options.slice(0, 60)
    // Match on any word in the path so "din" finds "Expense > Personal > Dining"
    // and "leisure din" finds "Expense > Leisure > Dining Out".
    const terms = q.split(/\s+/)
    return options.filter(p => {
      const hay = p.toLowerCase()
      return terms.every(t => hay.includes(t))
    }).slice(0, 60)
  }, [query, options])

  useEffect(() => { setHighlight(0) }, [query])

  function keyDown(e) {
    if (e.key === 'Escape') { e.preventDefault(); onCancel(); return }
    if (e.key === 'ArrowDown') { e.preventDefault(); setHighlight(h => Math.min(h + 1, matches.length - 1)); return }
    if (e.key === 'ArrowUp') { e.preventDefault(); setHighlight(h => Math.max(h - 1, 0)); return }
    if (e.key === 'Enter') {
      e.preventDefault()
      const pick = matches[highlight]
      if (pick && pick !== value) onPick(pick)
      else onCancel()
    }
  }

  // Flip upward when there is not enough room below, so the last rows of a long
  // table can still be edited.
  const MENU_MAX = 260
  const openUp = anchor ? (window.innerHeight - anchor.bottom < MENU_MAX + 16 && anchor.top > MENU_MAX) : false

  const menu = anchor ? createPortal(
    <div ref={menuRef} style={{position:'fixed', zIndex:400,
                 left: Math.max(8, Math.min(anchor.left, window.innerWidth - 300)),
                 top: openUp ? undefined : anchor.bottom + 3,
                 bottom: openUp ? (window.innerHeight - anchor.top + 3) : undefined,
                 minWidth: Math.max(280, anchor.width), maxHeight: MENU_MAX,
                 overflowY:'auto', background:'var(--popover, var(--card))',
                 border:'1px solid var(--border)', borderRadius:6,
                 boxShadow:'0 8px 24px rgba(0,0,0,.35)'}}
         onMouseDown={e => e.stopPropagation()}>
      {matches.length === 0 ? (
        <div style={{padding:'8px 10px', fontSize:12, color:'var(--muted-foreground)'}}>
          No category matches “{query}”
        </div>
      ) : matches.map((p, i) => (
        <div key={p}
             onMouseEnter={() => setHighlight(i)}
             onMouseDown={e => { e.preventDefault(); e.stopPropagation(); onPick(p) }}
             style={{padding:'6px 10px', fontSize:12, cursor:'pointer', whiteSpace:'nowrap',
                     background: i === highlight ? 'var(--accent, rgba(255,255,255,.07))' : 'transparent',
                     fontWeight: p === value ? 700 : 400}}>
          {p}
        </div>
      ))}
    </div>, document.body) : null

  return (
    // mousedown is stopped, not just click: the transactions row selects itself on
    // mousedown, which fires FIRST and would toggle the row every time you aimed
    // at the picker.
    <div ref={wrapRef} style={{position:'relative', minWidth:230}}
         onMouseDown={e => e.stopPropagation()}
         onClick={e => e.stopPropagation()}>
      <input ref={inputRef} className="form-input" style={{height:28, fontSize:12, width:'100%'}}
             placeholder="Type to filter categories…" value={query}
             onChange={e => setQuery(e.target.value)} onKeyDown={keyDown} />
      {menu}
    </div>
  )
}

function TxnTable({ txns, compact }) {
  const [whyTxn, setWhyTxn] = useState(null)
  if (!txns.length) return <div className="card-body" style={{color:"var(--muted-foreground)", textAlign:"center", padding:24}}>No transactions found</div>
  return (
    <>
    <table className="tbl">
      <thead>
        <tr>
          <th>Date</th>
          <th>Merchant</th>
          {compact ? null : <th>Description</th>}
          {!compact && <th>Account</th>}
          <th>Category</th>
          <th className="text-right">Amount</th>
          <th style={{width:36}}></th>
        </tr>
      </thead>
      <tbody>
        {txns.map(t => (
          <tr key={t.id}>
            <td className="mono" style={{fontSize:12}}>{t.posted_date?.slice(5) || ''}</td>
            <td>
              <div className="cell-merchant" style={{display:"flex", alignItems:"center", gap:6}}>
                <span>{t.merchant || '—'}</span>
                {t.flagged ? (
                  <span style={{
                    fontSize:10, padding:'1px 6px', borderRadius:999,
                    background: t.flag_reason === 'fraud' ? 'rgba(239,68,68,0.2)' : 'rgba(251,191,36,0.2)',
                    color: t.flag_reason === 'fraud' ? '#ef4444' : '#fbbf24',
                    fontWeight:600, textTransform:'uppercase', letterSpacing:0.5,
                    whiteSpace:'nowrap',
                  }}>{t.flag_reason === 'fraud' ? 'Fraud' : t.flag_reason === 'refund-pair' ? 'Refund' : 'Flagged'}</span>
                ) : null}
              </div>
            </td>
            {compact ? null : <td style={{fontSize:12, color:"var(--muted-foreground)", maxWidth:200, overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap"}}>{t.description || '—'}</td>}
            {!compact && <td style={{fontSize:12, color:"var(--muted-foreground)"}}>{t.account_id || ''}</td>}
            <td><span className="cell-cat">{t.category_path || 'Uncategorized'}</span></td>
            <td className={`text-right mono ${t.amount_cents >= 0 ? 'amt-pos' : 'amt-neg'}`}>
              {fmtCurrency(t.amount_cents)}
            </td>
            <td style={{textAlign:"right"}}>
              <button
                className="why-btn"
                title="Why this category?"
                aria-label={`Why category for ${t.merchant || 'transaction'}`}
                data-testid={`why-btn-${t.id}`}
                onClick={() => setWhyTxn(t)}
              >
                <Icon name="HelpCircle" size={14}/>
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
    {whyTxn && <WhyPanel txn={whyTxn} onClose={() => setWhyTxn(null)} />}
    </>
  )
}

function WhyPanel({ txn, onClose }) {
  const [rule, setRule] = useState(null)
  const [ruleErr, setRuleErr] = useState(null)
  useEffect(() => {
    if (!txn.applied_rule_id) { setRule(null); return }
    apiFetch(`/rules/${txn.applied_rule_id}`)
      .then(r => setRule(r))
      .catch(e => setRuleErr(e.message))
  }, [txn.applied_rule_id])

  const conf = txn.confidence_score
  const confPct = conf == null ? '—' : (conf * 100).toFixed(0) + '%'
  const confColor = conf == null ? 'var(--muted-foreground)' : (conf >= 0.85 ? 'var(--success, #22c55e)' : conf >= 0.6 ? 'var(--warning, #eab308)' : 'var(--danger, #ef4444)')

  return (
    <div className="modal-overlay" onClick={onClose} data-testid="why-panel">
      <div className="modal" onClick={e => e.stopPropagation()} style={{maxWidth:520}}>
        <div className="modal-header">
          <div className="modal-title">Why this category?</div>
          <button className="btn btn--ghost btn--sm" onClick={onClose}><Icon name="X" size={14}/></button>
        </div>
        <div className="modal-body">
          <div style={{marginBottom:14}}>
            <div style={{fontSize:13, color:"var(--muted-foreground)"}}>Transaction</div>
            <div style={{fontWeight:500}}>{txn.merchant || '—'} · <span className="mono">{fmtCurrency(txn.amount_cents)}</span></div>
            <div style={{fontSize:12, color:"var(--muted-foreground)"}}>{txn.posted_date} · {txn.account_id}</div>
          </div>
          <div style={{marginBottom:14}}>
            <div style={{fontSize:13, color:"var(--muted-foreground)"}}>Category</div>
            <div className="cell-cat" data-testid="why-category">{txn.category_path || 'Uncategorized'}</div>
          </div>
          <div style={{marginBottom:14}}>
            <div style={{fontSize:13, color:"var(--muted-foreground)"}}>Confidence</div>
            <div data-testid="why-confidence" style={{color:confColor, fontWeight:600}}>{confPct}</div>
          </div>
          <div style={{marginBottom:14}}>
            <div style={{fontSize:13, color:"var(--muted-foreground)"}}>Reasoning</div>
            <div data-testid="why-reason" style={{whiteSpace:"pre-wrap"}}>{txn.categorization_reason || <em>No reasoning recorded.</em>}</div>
          </div>
          {txn.applied_rule_id != null && (
            <div style={{marginBottom:14, padding:12, background:"var(--secondary)", borderRadius:6}}>
              <div style={{fontSize:13, color:"var(--muted-foreground)", marginBottom:4}}>Applied rule #{txn.applied_rule_id}</div>
              {ruleErr && <div style={{color:"var(--danger, #ef4444)"}}>Could not load rule: {ruleErr}</div>}
              {rule && (
                <div data-testid="why-rule">
                  <div style={{fontSize:13}}><strong>Pattern:</strong> <span className="mono">{rule.pattern}</span></div>
                  <div style={{fontSize:13}}><strong>Description:</strong> {rule.description || '—'}</div>
                  <div style={{fontSize:13}}><strong>Affected transactions:</strong> {rule.affected_count}</div>
                  <div style={{fontSize:12, color:"var(--muted-foreground)"}}>Created {rule.created_at}</div>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function TopSpendCard({ categories, period, onDrill }) {
  const expenses = useMemo(() => {
    return categories.filter(c => c.expense_cents > 0)
      .sort((a, b) => b.expense_cents - a.expense_cents)
  }, [categories])

  const totalExp = expenses.reduce((s, c) => s + c.expense_cents, 0)

  return (
    <div className="card">
      <div className="section-head">
        <div>
          <div className="h">Top Spending</div>
          <div className="sub">{expenses.length} categories with expense</div>
        </div>
        <Icon name="PieChart" size={14} color="var(--muted-foreground)"/>
      </div>
      <div style={{padding: "8px 0", maxHeight:600, overflowY:'auto'}}>
        {expenses.map(c => {
          const pct = totalExp ? (c.expense_cents / totalExp) * 100 : 0
          const catName = c.category_path?.split(' > ').pop() || c.category_path
          return (
            <div key={c.category_path}
                 style={{padding:"10px 20px", cursor:"pointer"}}
                 className="topspend-row"
                 onClick={() => onDrill && onDrill(c.category_path)}
                 onMouseEnter={e => e.currentTarget.style.background = "var(--secondary)"}
                 onMouseLeave={e => e.currentTarget.style.background = ""}>
              <div style={{display:"flex", justifyContent:"space-between", alignItems:"baseline", marginBottom:6}}>
                <div style={{display:"flex", alignItems:"center", gap:8}}>
                  <span className="swatch" style={{background: "#a78bfa"}}/>
                  <span style={{fontSize:13.5, fontWeight:500}}>{catName}</span>
                  <span style={{fontSize:10, color:'var(--muted-foreground)'}}>{c.category_path}</span>
                </div>
                <div className="mono" style={{fontSize:13}}>{fmtCurrency(c.expense_cents)}</div>
              </div>
              <div style={{position:"relative", height:5, background:"var(--secondary)", borderRadius:3, overflow:"hidden"}}>
                <div style={{position:"absolute", inset:0, width: pct + "%", background: "#a78bfa", borderRadius:3}}/>
              </div>
            </div>
          )
        })}
        {!expenses.length && (
          <div style={{padding:"20px", textAlign:"center", color:"var(--muted-foreground)"}}>No expense data</div>
        )}
      </div>
    </div>
  )
}

// ───── Budgets Page ─────
function BudgetsPage({ periodId }) {
  const { dataVersion } = useContext(AppContext)
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [showRollover, setShowRollover] = useState(false)
  const [rolloverData, setRolloverData] = useState(null)
  const [rolloverLoading, setRolloverLoading] = useState(false)
  const [rolloverMsg, setRolloverMsg] = useState(null)
  const [insights, setInsights] = useState([])
  const [insightsLoading, setInsightsLoading] = useState(true)
  // "New budget" was a dead button — rendered, styled, and wired to nothing.
  const [showNew, setShowNew] = useState(false)
  const [newForm, setNewForm] = useState({
    category_path: '', budget_type: 'absolute', amount: '', percent: '', period: 'month', label: '',
  })
  const [newErr, setNewErr] = useState(null)
  const [saving, setSaving] = useState(false)

  async function handleNewSubmit(e) {
    e.preventDefault()
    setNewErr(null)
    if (!newForm.category_path.trim()) { setNewErr('Pick a category'); return }
    const absolute = newForm.budget_type === 'absolute'
    if (absolute && !newForm.amount) { setNewErr('Enter a monthly amount'); return }
    if (!absolute && !newForm.percent) { setNewErr('Enter a percent of income'); return }
    setSaving(true)
    try {
      await apiPost('/budgets', {
        category_path: newForm.category_path.trim(),
        budget_type: newForm.budget_type,
        // The API takes cents; the form takes dollars, which is what a person types.
        amount_cents: absolute ? Math.round(parseFloat(newForm.amount) * 100) : null,
        percent: absolute ? null : parseFloat(newForm.percent),
        period: newForm.period,
        label: newForm.label.trim() || newForm.category_path.trim(),
      })
      setShowNew(false)
      setNewForm({ category_path: '', budget_type: 'absolute', amount: '', percent: '', period: 'month', label: '' })
      setReloadKey(k => k + 1)
    } catch (err) {
      setNewErr(String(err.message || err))
    } finally {
      setSaving(false)
    }
  }

  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    setLoading(true)
    setInsightsLoading(true)
    const period = resolvePeriod(periodId)
    const ps = period.start.toISOString().slice(0, 10)
    const pe = period.end.toISOString().slice(0, 10)
    apiFetch('/budgets/status?period_start=' + ps + '&period_end=' + pe)
      .then(d => { setData(d); setLoading(false) })
      .catch(() => setLoading(false))
    apiFetch('/budgets/intelligence?period_start=' + ps + '&period_end=' + pe)
      .then(d => { setInsights(d.insights || []); setInsightsLoading(false) })
      .catch(() => { setInsights([]); setInsightsLoading(false) })
  }, [periodId, dataVersion, reloadKey])

  const overCount = data?.budgets ? data.budgets.filter(r => r.pct_used >= 100).length : 0
  const totalBudgeted = data?.budgets ? data.budgets.reduce((s, r) => s + r.target_cents, 0) : 0
  const totalSpent = data?.budgets ? data.budgets.reduce((s, r) => s + r.spent_cents, 0) : 0

  async function handleRolloverOpen() {
    const period = resolvePeriod(periodId)
    const fromStart = fmtDateParam(period.start)
    const fromEnd = fmtDateParam(period.end)
    // Compute next period: add 1 month to the start
    const toStart = new Date(period.start.getFullYear(), period.start.getMonth() + 1, 1)
    const toEnd = new Date(period.start.getFullYear(), period.start.getMonth() + 2, 0, 23, 59, 59)
    setRolloverLoading(true)
    setRolloverMsg(null)
    try {
      const res = await apiPost('/budgets/rollover', {
        from_period_start: fromStart,
        from_period_end: fromEnd,
        to_period_start: fmtDateParam(toStart),
        to_period_end: fmtDateParam(toEnd),
      })
      setRolloverData(res)
      setShowRollover(true)
    } catch (err) {
      setRolloverMsg({ type: 'err', text: err.message })
    } finally {
      setRolloverLoading(false)
    }
  }

  async function handleRolloverAccept() {
    if (!rolloverData) return
    setRolloverLoading(true)
    try {
      const budgets = rolloverData.suggested.map(s => ({
        category_path: s.category_path,
        budget_type: s.budget_type,
        amount_cents: s.amount_cents,
        percent: s.percent,
        label: s.label,
      }))
      await apiPost('/budgets/rollover/accept', {
        budgets,
        to_period_start: rolloverData.to_period.start,
        to_period_end: rolloverData.to_period.end,
      })
      setShowRollover(false)
      setRolloverData(null)
      setRolloverMsg({ type: 'ok', text: 'Budgets rolled over to next period' })
      // Refresh data
      const period = resolvePeriod(periodId)
      const ps = fmtDateParam(period.start)
      const pe = fmtDateParam(period.end)
      const d = await apiFetch('/budgets/status?period_start=' + ps + '&period_end=' + pe)
      setData(d)
    } catch (err) {
      setRolloverMsg({ type: 'err', text: err.message })
    } finally {
      setRolloverLoading(false)
    }
  }

  if (loading) {
    return (
      <div className="page">
        <div className="subtitle">Loading budgets...</div>
      </div>
    )
  }

  const rows = data?.budgets || []

  return (
    <div className="page">
      {rolloverMsg && (
        <div className={`msg-banner ${rolloverMsg.type}`} style={{marginBottom:12}}>{rolloverMsg.text}</div>
      )}
      <div className="page-intro">
        <div className="subtitle">{rows.length} active budgets · {overCount} over the line · pacing dot shows expected progress through the period</div>
        <div className="page-intro-actions">
          <button className="btn btn--ghost" onClick={handleRolloverOpen} disabled={rolloverLoading}>
            <Icon name="Upload" size={14}/> Rollover
          </button>
          <button className="btn btn--primary" onClick={() => setShowNew(true)}>
            <Icon name="Plus" size={14}/> New budget
          </button>
        </div>
      </div>

      <div className="kpi-grid" style={{gridTemplateColumns:"repeat(3, 1fr)"}}>
        <KpiCard label="Budgeted this period" value={fmtCurrency(totalBudgeted)} sub={<span><Icon name="Target" size={12}/> Across {rows.length} envelopes</span>}/>
        <KpiCard label="Spent so far" value={fmtCurrency(totalSpent)}
                 sub={<span>{fmtPct((totalSpent / totalBudgeted) * 100 - 100)} of envelope</span>}
                 valueClass={totalSpent > totalBudgeted ? "neg" : ""}/>
        <KpiCard label="Remaining" value={fmtCurrency(Math.max(0, totalBudgeted - totalSpent))}
                 valueClass={totalBudgeted - totalSpent < 0 ? "neg" : "pos"}/>
      </div>

      <div className="card">
        <div className="section-head">
          <div>
            <div className="h">Envelopes</div>
            <div className="sub">Subtle pacing: the dot shows where you'd be on track; overshoot bleeds beyond 100% with diagonal hatch.</div>
          </div>
        </div>
        <div>
          {rows.map(r => <BudgetRow key={r.id} r={r}/>)}
        </div>
      </div>

      <IntelligencePanel insights={insights} loading={insightsLoading}/>

      {showNew && (
        <div className="modal-overlay" onClick={() => setShowNew(false)}>
          <div className="modal" onClick={e => e.stopPropagation()} style={{maxWidth:560}}>
            <div className="modal-header">
              <div className="modal-title">New Budget</div>
              <button className="btn btn--ghost btn--sm" onClick={() => setShowNew(false)}>
                <Icon name="X" size={14}/>
              </button>
            </div>
            <form onSubmit={handleNewSubmit}>
              <div className="modal-body">
                <div className="form-group">
                  <label className="form-label">Category *</label>
                  <input className="form-input" value={newForm.category_path}
                         onChange={e => setNewForm(f => ({...f, category_path: e.target.value}))}
                         placeholder="e.g. Expense > Personal > Groceries" />
                </div>
                <div className="form-row">
                  <div className="form-group">
                    <label className="form-label">Type</label>
                    <select className="form-input" value={newForm.budget_type}
                            onChange={e => setNewForm(f => ({...f, budget_type: e.target.value}))}>
                      <option value="absolute">Fixed amount</option>
                      <option value="percent_of_income">Percent of income</option>
                    </select>
                  </div>
                  {newForm.budget_type === 'absolute' ? (
                    <div className="form-group">
                      <label className="form-label">Amount ($) *</label>
                      <input className="form-input" type="number" step="0.01" value={newForm.amount}
                             onChange={e => setNewForm(f => ({...f, amount: e.target.value}))}
                             placeholder="e.g. 600" />
                    </div>
                  ) : (
                    <div className="form-group">
                      <label className="form-label">Percent of income *</label>
                      <input className="form-input" type="number" step="0.1" value={newForm.percent}
                             onChange={e => setNewForm(f => ({...f, percent: e.target.value}))}
                             placeholder="e.g. 10" />
                    </div>
                  )}
                </div>
                <div className="form-row">
                  <div className="form-group">
                    <label className="form-label">Period</label>
                    <select className="form-input" value={newForm.period}
                            onChange={e => setNewForm(f => ({...f, period: e.target.value}))}>
                      <option value="month">Monthly</option>
                      <option value="week">Weekly</option>
                      <option value="year">Yearly</option>
                    </select>
                  </div>
                  <div className="form-group">
                    <label className="form-label">Label</label>
                    <input className="form-input" value={newForm.label}
                           onChange={e => setNewForm(f => ({...f, label: e.target.value}))}
                           placeholder="Defaults to the category" />
                  </div>
                </div>
                {newErr && <div className="form-group" style={{color:"var(--destructive)"}}>{newErr}</div>}
              </div>
              <div className="modal-foot">
                <button type="button" className="btn btn--outline" onClick={() => setShowNew(false)}>Cancel</button>
                <button type="submit" className="btn btn--primary" disabled={saving}>
                  {saving ? 'Saving…' : 'Create budget'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {showRollover && rolloverData && (
        <div className="modal-overlay" onClick={() => setShowRollover(false)}>
          <div className="modal" onClick={e => e.stopPropagation()} style={{maxWidth:600}}>
            <div className="modal-header">
              <div className="modal-title">Rollover Budgets</div>
              <button className="btn btn--ghost btn--sm" onClick={() => setShowRollover(false)}><Icon name="X" size={14}/></button>
            </div>
            <div className="modal-body">
              <div style={{marginBottom:16}}>
                <div className="sub">
                  From: {fmtDate(new Date(rolloverData.from_period.start))} – {fmtDate(new Date(rolloverData.from_period.end))}
                  &nbsp;→&nbsp;
                  To: {fmtDate(new Date(rolloverData.to_period.start))} – {fmtDate(new Date(rolloverData.to_period.end))}
                </div>
              </div>

              <div className="h" style={{marginBottom:12}}>Previous Period</div>
              {rolloverData.previous.map((p, i) => (
                <div key={i} className="budget-row" style={{gridTemplateColumns:"1fr 100px 100px"}}>
                  <div>
                    <div className="budget-name">{p.label || p.category_path}</div>
                    <div className="budget-period">Target: {fmtCurrency(p.target_cents, {decimals:0})} · Spent: {fmtCurrency(p.spent_cents, {decimals:0})}</div>
                  </div>
                  <div className="budget-amt">
                    <div className={`v mono`} style={p.variance > 0 ? {color:'var(--destructive)'} : {}}>
                      {p.variance > 0 ? '+' : ''}{fmtCurrency(p.variance, {decimals:0})}
                    </div>
                    <div className="label">{p.variance > 0 ? 'over' : 'under'}</div>
                  </div>
                  <div className="budget-amt">
                    <div className="v mono">{fmtPct(p.variance / Math.max(p.target_cents, 1) * 100, 0)}</div>
                    <div className="label">vs target</div>
                  </div>
                </div>
              ))}

              <div className="h" style={{margin:'20px 0 12px'}}>Suggested Next Period</div>
              {rolloverData.suggested.map((s, i) => (
                <div key={i} className="budget-row" style={{gridTemplateColumns:"1fr 100px"}}>
                  <div>
                    <div className="budget-name">{s.label || s.category_path}</div>
                    <div className="budget-period" style={{fontSize:12, color:'var(--muted-foreground)', marginTop:2}}>
                      {s.reason}
                    </div>
                  </div>
                  <div className="budget-amt">
                    <div className="v mono">{s.budget_type === 'absolute' ? fmtCurrency(s.amount_cents, {decimals:0}) : s.percent + '%'}</div>
                    <div className="label">{s.budget_type === 'absolute' ? 'absolute' : 'of income'}</div>
                  </div>
                </div>
              ))}
            </div>
            <div className="modal-footer" style={{display:'flex', justifyContent:'flex-end', gap:8, padding:'12px 20px', borderTop:'1px solid var(--border)'}}>
              <button className="btn btn--ghost" onClick={() => setShowRollover(false)}>Cancel</button>
              <button className="btn btn--primary" onClick={handleRolloverAccept} disabled={rolloverLoading}>
                {rolloverLoading ? 'Creating...' : 'Accept All'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function BudgetRow({ r }) {
  const isOver = r.pct_used >= 100
  const isOnPace = r.pct_used <= (r.expected_pct || 0) + 5
  const remaining = r.target_cents - r.spent_cents

  const MAX_PCT = 130
  const budgetMarkPct = (100 / MAX_PCT) * 100
  const fillPct = Math.min(MAX_PCT, r.pct_used)
  const fillWidthPct = (fillPct / MAX_PCT) * 100
  const overshootStart = budgetMarkPct
  const overshootEnd = isOver ? Math.min(100, fillWidthPct) : budgetMarkPct
  const pacingPct = Math.min(MAX_PCT, r.expected_pct || 0) / MAX_PCT * 100
  const fillClass = isOver ? "over" : (isOnPace ? "ok" : "warn")

  return (
    <div className="budget-row">
      <div>
        <div className="budget-name">
          <span className="swatch" style={{background: r.pct_used >= 100 ? 'var(--destructive)' : (isOnPace ? '#4ade80' : '#facc15')}}/>
          {r.label || r.category_path}
        </div>
        <div className="budget-period">
          {r.category_path} · {r.period === "week" ? "Weekly" : r.period === "year" ? "Yearly" : "Monthly"} · {fmtDate(new Date(r.period_start))} → {fmtDate(new Date(r.period_end))}
        </div>
      </div>
      <div className="budget-amt">
        <div className="v mono" style={isOver ? {color:"var(--destructive)"} : null}>{fmtCurrency(r.spent_cents, { decimals: 0 })}</div>
        <div className="label">of {fmtCurrency(r.target_cents, {decimals:0})}</div>
      </div>
      <div className="budget-amt">
        <div className="v mono" style={remaining < 0 ? {color:"var(--destructive)"} : null}>
          {remaining >= 0 ? fmtCurrency(remaining, {decimals:0}) : "−" + fmtCurrency(Math.abs(remaining), {decimals:0})}
        </div>
        <div className="label">{remaining >= 0 ? "remaining" : "over"}</div>
      </div>
      <div>
        <div className="budget-track" style={{height: 10, position:"relative", overflow:"hidden"}}>
          <div className={`budget-fill ${fillClass}`}
               style={{
                 width: (isOver ? budgetMarkPct : fillWidthPct) + "%",
                 borderRadius: 4,
               }}/>
          {isOver && (
            <div className="budget-overshoot"
                 style={{
                   left: overshootStart + "%",
                   width: (overshootEnd - overshootStart) + "%",
                   borderRadius: 0,
                 }}/>
          )}
          <div style={{
            position:"absolute", top:-3, bottom:-3, left: budgetMarkPct + "%",
            width: 2, background: "var(--foreground)", opacity: 0.55, borderRadius: 1,
          }} title="Budget limit"/>
          <div className="budget-pacing"
               style={{left: pacingPct + "%", opacity: 0.35, top: -2, bottom: -2}}
               title={`On pace: ${(r.expected_pct || 0).toFixed(0)}%`}/>
        </div>
        <div className={`budget-status ${isOver ? "over" : ""}`} style={{marginTop:6, fontSize:11}}>
          {isOver
            ? <>Over by <span className="mono">{fmtCurrency(r.forecast_overrun_cents, {decimals:0})}</span></>
            : <>{r.pct_used.toFixed(0)}% used · {isOnPace ? "on pace" : "ahead of pace"}</>
          }
        </div>
      </div>
    </div>
  )
}

function IntelligencePanel({ insights, loading }) {
  const kindMeta = {
    'over-budget-risk':     { icon: 'AlertTriangle', label: 'Over-budget risk' },
    'recurring-waste':      { icon: 'Repeat',        label: 'Recurring waste' },
    'savings-opportunity':  { icon: 'Sparkles',      label: 'Savings opportunity' },
    'historical-anomaly':   { icon: 'TrendingUp',    label: 'Historical anomaly' },
  }
  const sevColor = {
    danger: 'var(--destructive)',
    warn: '#facc15',
    info: '#60a5fa',
  }

  return (
    <div className="card" style={{marginTop:16}}>
      <div className="section-head">
        <div>
          <div className="h"><Icon name="Sparkles" size={14}/> Budget Intelligence</div>
          <div className="sub">Mid-period risks, recurring-charge waste, savings slack, and historical spikes — each cites the transactions or budgets it found.</div>
        </div>
      </div>
      <div className="card-body">
        {loading && <div className="sub">Computing insights…</div>}
        {!loading && insights.length === 0 && (
          <div className="sub">No insights yet for this period. Add budgets or wait for more data.</div>
        )}
        {!loading && insights.length > 0 && (
          <div style={{display:'flex', flexDirection:'column', gap:10}}>
            {insights.map((ins, i) => {
              const meta = kindMeta[ins.kind] || { icon: 'HelpCircle', label: ins.kind }
              const color = sevColor[ins.severity] || 'var(--muted-foreground)'
              const txnCount = ins.evidence?.transaction_ids?.length || 0
              const budgetCount = ins.evidence?.budget_ids?.length || 0
              return (
                <div key={i} className="insight-row" style={{
                  display:'grid',
                  gridTemplateColumns:'auto 1fr auto',
                  gap:12,
                  alignItems:'flex-start',
                  padding:'12px 14px',
                  border:'1px solid var(--border)',
                  borderLeft:`3px solid ${color}`,
                  borderRadius:8,
                  background:'rgba(255,255,255,0.02)',
                }}>
                  <div style={{color, paddingTop:2}}><Icon name={meta.icon} size={16}/></div>
                  <div>
                    <div style={{display:'flex', gap:8, alignItems:'center', marginBottom:4, flexWrap:'wrap'}}>
                      <span style={{
                        fontSize:11, padding:'2px 8px', borderRadius:999,
                        background:'rgba(255,255,255,0.06)', color, fontWeight:600,
                        textTransform:'uppercase', letterSpacing:0.5,
                      }}>{meta.label}</span>
                      <span className="sub" style={{fontSize:12}}>{ins.category}</span>
                    </div>
                    <div style={{fontSize:13.5, lineHeight:1.5}}>{ins.message}</div>
                    <div className="sub" style={{fontSize:12, marginTop:6, fontStyle:'italic'}}>
                      → {ins.suggested_action}
                    </div>
                  </div>
                  <div style={{display:'flex', flexDirection:'column', gap:4, alignItems:'flex-end', fontSize:11, color:'var(--muted-foreground)'}}>
                    {txnCount > 0 && (
                      <span><Icon name="Receipt" size={11}/> {txnCount} txn{txnCount > 1 ? 's' : ''}</span>
                    )}
                    {budgetCount > 0 && (
                      <span><Icon name="Target" size={11}/> {budgetCount} budget</span>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}

// ───── Category Tree Select ─────
function CategoryTreeSelect({ nodes, depth, onSelect, currentPath }) {
  return (
    <div style={{paddingLeft: depth * 16}}>
      {nodes.map(n => (
        <div key={n.id} style={{margin:'2px 0'}}>
          <span
            onClick={() => onSelect(n.path)}
            style={{cursor:'pointer', fontWeight: currentPath === n.path ? 700 : 400, color: currentPath === n.path ? 'var(--primary)' : 'var(--foreground)'}}
            onMouseEnter={e => e.currentTarget.style.opacity = 0.7}
            onMouseLeave={e => e.currentTarget.style.opacity = 1}
          >
            {n.name}
          </span>
          {n.children && n.children.length > 0 && (
            <CategoryTreeSelect nodes={n.children} depth={depth + 1} onSelect={onSelect} currentPath={currentPath} />
          )}
        </div>
      ))}
    </div>
  )
}

// ───── Transactions Page ─────
// The per-item rows a composite charge was split into, shown inline under the
// parent. The parent itself is neutralised on purpose — its money is carried by
// these children — so this is the only correct place to categorize a split
// charge. Making that visible is the fix for people (reasonably) trying to
// recategorize the parent and getting a refusal they did not expect.
function SplitChildRows({ parent, catPaths, categoryColors, onChanged }) {
  const [rows, setRows] = useState(null)
  const [error, setError] = useState(null)
  const [editing, setEditing] = useState(null)

  useEffect(() => {
    let live = true
    apiFetch(`/transactions/${parent.id}/split-items`)
      .then(d => { if (live) setRows(d.items || []) })
      .catch(e => { if (live) setError(e.message) })
    return () => { live = false }
  }, [parent.id])

  async function setChildCategory(childId, category) {
    setEditing(null)
    try {
      await apiFetch(`/transactions/${childId}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ category_path: category }),
      })
      setRows(rs => rs.map(r => r.id === childId ? { ...r, category_path: category } : r))
      onChanged?.()
    } catch (e) {
      setError(e.message)
    }
  }

  const childSum = (rows || []).reduce((s, r) => s + (r.amount_cents || 0), 0)
  const balanced = rows && childSum === parent.amount_cents

  return (
    <tr>
      <td/>
      <td colSpan={7} style={{padding:'6px 0 10px'}}>
        <div style={{borderLeft:'2px solid color-mix(in oklab, var(--primary) 45%, transparent)',
                     paddingLeft:12, marginLeft:2}}>
          <div className="subtitle" style={{fontSize:11, marginBottom:6}}>
            {parent.split_child_count} item{parent.split_child_count === 1 ? '' : 's'} making up{' '}
            {fmtCurrency(parent.amount_cents, {full:true})}. Categorize the items — the
            combined charge above stays out of your totals so nothing counts twice.
          </div>
          {error && <div className="msg-banner err">{error}</div>}
          {!rows && <div className="subtitle" style={{fontSize:11}}><Icon name="Loader" size={11}/> Loading items…</div>}
          {rows && (
            <table className="tbl" style={{fontSize:12}}>
              <tbody>
                {rows.map(r => (
                  <tr key={r.id}>
                    <td style={{maxWidth:340, overflow:'hidden', textOverflow:'ellipsis',
                                whiteSpace:'nowrap'}} title={r.merchant || ''}>
                      {(r.merchant || '').replace(/^Amazon:\s*/, '') || '—'}
                    </td>
                    <td className="no-row-select" style={{width:250}}>
                      {editing === r.id ? (
                        <CategoryPicker value={r.category_path} options={catPaths}
                                        onPick={(p) => setChildCategory(r.id, p)}
                                        onCancel={() => setEditing(null)} />
                      ) : (
                        <span className="cell-cat is-editable" title="Click to re-categorize this item"
                              onMouseDown={(e) => { e.stopPropagation(); setEditing(r.id) }}>
                          <span style={{fontSize:12, display:'inline-flex', alignItems:'center'}}>
                            <CategoryDot color={categoryColors[r.category_path]}/>
                            {r.category_path || 'Uncategorized'}
                          </span>
                          <Icon name="ChevronDown" size={11}/>
                        </span>
                      )}
                    </td>
                    <td className={`text-right mono ${r.amount_cents >= 0 ? 'amt-pos' : 'amt-neg'}`}
                        style={{width:110}}>
                      {fmtSignedCurrency(r.amount_cents)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {/* The split's core invariant. If it ever breaks, the money is being
              double-counted or lost, and silence would be the worst outcome. */}
          {rows && !balanced && (
            <div className="msg-banner err" style={{marginTop:6}}>
              These items total {fmtCurrency(childSum, {full:true})} but the charge is{' '}
              {fmtCurrency(parent.amount_cents, {full:true})} — the split is unbalanced.
            </div>
          )}
        </div>
      </td>
    </tr>
  )
}

// ───── "Apply this category to the others like it?" ─────
// The point of this modal is speed with an escape hatch: every match is checked
// by default so the common answer is one click, but nothing is written until the
// user confirms and every row can be individually unticked first.
function SimilarApplyModal({ prompt, categoryColors, onToggle, onToggleAll, onSetApplyAll,
                             onSetCreateRule, onApply, onDismiss }) {
  const p = prompt
  const allChecked = p.matches.length > 0 && p.checked.size === p.matches.length
  const applyCount = p.applyAll ? p.count : p.checked.size
  return (
    <div className="modal-overlay" onClick={() => !p.applying && onDismiss(false)}>
      <div className="modal" onClick={e => e.stopPropagation()} style={{maxWidth:680, width:680}}>
        <div className="modal-header">
          <div>
            <div className="modal-title">
              Apply <CategoryDot color={categoryColors[p.category]}/> {p.category} to {p.count} more?
            </div>
            <div className="subtitle" style={{fontSize:12, marginTop:2}}>
              {p.count} other transaction{p.count === 1 ? '' : 's'} look like <strong>{p.matchLabel}</strong>.
            </div>
          </div>
          <button className="btn btn--ghost btn--sm" disabled={p.applying} onClick={() => onDismiss(false)}>
            <Icon name="X" size={14}/>
          </button>
        </div>

        <div className="modal-body" style={{gap:10}}>
          {p.error && <div className="msg-banner err">{p.error}</div>}

          <div style={{display:'flex', alignItems:'center', gap:10, fontSize:12}}>
            <label style={{display:'flex', alignItems:'center', gap:6, cursor:'pointer'}}>
              <input type="checkbox" checked={allChecked} disabled={p.applyAll}
                     onChange={e => onToggleAll(e.target.checked)}
                     style={{accentColor:'var(--primary)'}}/>
              {allChecked ? 'Deselect all' : 'Select all'}
            </label>
            <span style={{color:'var(--muted-foreground)'}}>
              {p.applyAll ? `all ${p.count} selected` : `${p.checked.size} of ${p.matches.length} selected`}
            </span>
          </div>

          {p.truncated && (
            <label style={{display:'flex', alignItems:'center', gap:6, fontSize:12, cursor:'pointer',
                           padding:'6px 10px', borderRadius:6,
                           background:'color-mix(in oklab, var(--primary) 10%, transparent)'}}>
              <input type="checkbox" checked={p.applyAll}
                     onChange={e => onSetApplyAll(e.target.checked)}
                     style={{accentColor:'var(--primary)'}}/>
              Showing the {p.matches.length} most recent. Apply to all {p.count} matches instead.
            </label>
          )}

          <div style={{maxHeight:300, overflowY:'auto', border:'1px solid var(--border)', borderRadius:6}}>
            <table className="tbl" style={{fontSize:12}}>
              <tbody>
                {p.matches.map(m => (
                  <tr key={m.id} style={{cursor: p.applyAll ? 'default' : 'pointer'}}
                      onClick={() => { if (!p.applyAll && !p.applying) onToggle(m.id) }}>
                    <td style={{width:30}}>
                      <input type="checkbox" checked={p.applyAll || p.checked.has(m.id)}
                             disabled={p.applyAll || p.applying}
                             onChange={() => onToggle(m.id)}
                             onClick={e => e.stopPropagation()}
                             style={{accentColor:'var(--primary)'}}/>
                    </td>
                    <td className="mono" style={{width:64, whiteSpace:'nowrap', color:'var(--muted-foreground)'}}>
                      {m.posted_date?.slice(5) || ''}
                    </td>
                    <td>{m.merchant || m.description || '—'}</td>
                    <td style={{color:'var(--muted-foreground)'}}>
                      <span style={{display:'inline-flex', alignItems:'center'}}>
                        <CategoryDot color={categoryColors[m.category_path]}/>
                        {m.category_path || 'Uncategorized'}
                      </span>
                    </td>
                    <td className={`text-right mono ${m.amount_cents >= 0 ? 'amt-pos' : 'amt-neg'}`}
                        style={{width:96}}>
                      {fmtSignedCurrency(m.amount_cents)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <label style={{display:'flex', alignItems:'center', gap:6, fontSize:12, cursor:'pointer'}}>
            <input type="checkbox" checked={p.createRule}
                   onChange={e => onSetCreateRule(e.target.checked)}
                   style={{accentColor:'var(--primary)'}}/>
            Remember this — future imports matching “{p.pattern}” get {p.category} automatically
          </label>
        </div>

        <div className="modal-foot">
          <button className="btn btn--ghost btn--sm" disabled={p.applying}
                  onClick={() => onDismiss(true)}
                  title={`Stop offering this for ${p.matchLabel} until you reload`}
                  style={{marginRight:'auto'}}>
            Don’t ask again for {p.matchLabel}
          </button>
          <button className="btn btn--outline" disabled={p.applying} onClick={() => onDismiss(false)}>
            No thanks
          </button>
          <button className="btn btn--primary" disabled={p.applying || applyCount === 0}
                  onClick={onApply}>
            {p.applying
              ? <><Icon name="Loader" size={13}/> Applying…</>
              : `Apply to ${applyCount}`}
          </button>
        </div>
      </div>
    </div>
  )
}

function TransactionsPage({ periodId, onDrillCategory, initialCategory }) {
  const { dataVersion, categoryColors, bumpDataVersion } = useContext(AppContext)
  const [txns, setTxns] = useState([])
  const [total, setTotal] = useState(0)
  const [totals, setTotals] = useState({})
  const [loading, setLoading] = useState(true)
  const [page, setPage] = useState(1)
  const [searchInput, setSearchInput] = useState('')
  const [searchQuery, setSearchQuery] = useState('')

  // Search-as-you-type (debounced). Previously the box only committed on Enter,
  // so typing appeared to do nothing at all — the table just sat there. Enter
  // still works and simply commits early.
  useEffect(() => {
    const handle = setTimeout(() => {
      const next = searchInput.trim()
      setSearchQuery(prev => {
        if (prev === next) return prev
        setPage(1)
        return next
      })
    }, 350)
    return () => clearTimeout(handle)
  }, [searchInput])
  const [accountFilter, setAccountFilter] = useState(null)
  const [categoryFilter, setCategoryFilter] = useState(initialCategory || '')
  // '' | 'income' | 'expense' | 'transfer' — a sign question, answered server-side.
  const [flowFilter, setFlowFilter] = useState('')
  const [sortCol, setSortCol] = useState('posted_date')
  const [sortDir, setSortDir] = useState('desc')
  const [descriptionFilter, setDescriptionFilter] = useState('')
  const [sourceFileFilter, setSourceFileFilter] = useState('')
  const [amountMin, setAmountMin] = useState('')
  const [amountMax, setAmountMax] = useState('')
  const [flaggedFilter, setFlaggedFilter] = useState('')
  const [showRaw, setShowRaw] = useState(false)
  const [selected, setSelected] = useState(new Set())
  const [bulkMsg, setBulkMsg] = useState(null)
  const [showAdd, setShowAdd] = useState(false)
  const [addForm, setAddForm] = useState({ merchant: '', amount_cents: '', posted_date: '', category_path: '', business_context: 'personal', notes: '', account_id: 'manual' })
  const [addMsg, setAddMsg] = useState(null)
  const [reclassified, setReclassified] = useState({})
  const [accounts, setAccounts] = useState([])
  const [whyTxn, setWhyTxn] = useState(null)
  const [showCatTree, setShowCatTree] = useState(false)
  const [catTree, setCatTree] = useState([])
  const [catPaths, setCatPaths] = useState([])
  const [editingCat, setEditingCat] = useState(null)
  const [bulkPicking, setBulkPicking] = useState(false)
  // Refunded purchases are a wash; hiding both halves declutters the list.
  // 'all' | 'hide' | 'only' — seeing just the refunded rows is how you find the
  // ones worth recategorizing (e.g. every charge from one disputed merchant).
  const [refundFilter, setRefundFilter] = useState('all')
  // "You just filed one Target charge under Groceries — the other 286 are
  // probably Groceries too." `similarPrompt` holds that offer; `suppressedKeys`
  // remembers the merchants you have said no to, so declining once stops the
  // nagging for the rest of the session.
  // Which split parent has its item rows expanded, if any.
  const [expandedSplit, setExpandedSplit] = useState(null)
  const [similarPrompt, setSimilarPrompt] = useState(null)
  const [similarLoading, setSimilarLoading] = useState(false)
  const suppressedKeys = useRef(new Set())
  const PAGE_SIZE = 50

  useEffect(() => {
    apiFetch('/categories/tree').then(d => setCatTree(d.tree || [])).catch(() => {})
    // Flat leaf paths drive the type-ahead picker.
    apiFetch('/categories')
      .then(d => {
        const all = (d.categories || []).map(c => c.path)
        const leaves = all.filter(p => !all.some(o => o !== p && o.startsWith(p + ' > ')))
        setCatPaths(leaves.sort())
      })
      .catch(() => {})
  }, [])

  function handleSort(col) {
    if (sortCol === col) {
      if (sortDir === 'asc') setSortDir('desc')
      else if (sortDir === 'desc') { setSortCol('posted_date'); setSortDir('desc') }
    } else {
      setSortCol(col)
      setSortDir('asc')
    }
    setPage(1)
  }

  function sortIndicator(col) {
    if (sortCol !== col) return ''
    return sortDir === 'asc' ? ' ▲' : ' ▼'
  }

  const fetchTxns = useCallback(() => {
    setLoading(true)
    const period = resolvePeriod(periodId)
    const ps = fmtDateParam(period.start)
    const pe = fmtDateParam(period.end)
    const params = new URLSearchParams({ page_size: PAGE_SIZE, page, start_date: ps, end_date: pe })
    params.set('sort', sortCol)
    params.set('dir', sortDir)
    if (searchQuery) params.set('search', searchQuery)
    if (refundFilter === 'hide') params.set('exclude_refunded', 'true')
    if (refundFilter === 'only') params.set('only_refunded', 'true')
    if (accountFilter) params.set('account_id', accountFilter)
    if (flowFilter) params.set('flow', flowFilter)
    if (categoryFilter) params.set('category_path', categoryFilter)
    if (descriptionFilter) params.set('description', descriptionFilter)
    if (sourceFileFilter) params.set('source_file', sourceFileFilter)
    if (amountMin) params.set('amount_min', amountMin)
    if (amountMax) params.set('amount_max', amountMax)
    if (flaggedFilter) params.set('flagged', flaggedFilter)
    apiFetch(`/transactions?${params}`)
      .then(d => {
        setTxns(d.transactions || [])
        setTotal(d.total || 0)
        setTotals(d.totals || {})
        setLoading(false)
      })
      .catch(() => setLoading(false))
  }, [periodId, page, searchQuery, accountFilter, categoryFilter, flowFilter, sortCol, sortDir,
      descriptionFilter, sourceFileFilter, amountMin, amountMax, flaggedFilter, refundFilter,
      dataVersion])

  useEffect(() => { fetchTxns() }, [fetchTxns])

  useEffect(() => {
    apiFetch('/accounts').then(d => setAccounts(Array.isArray(d) ? d : [])).catch(() => {})
  }, [])

  const totalPages = Math.ceil(total / PAGE_SIZE)
  // Server-side totals over the WHOLE filtered set. Summing the 50 rows on this
  // page and printing the result next to the full `total` count made the line lie:
  // "3,767 transactions · $2,017 in · $1,962 out" was a real count beside a
  // one-page figure.
  const summary = { income: totals.income_cents || 0, expense: totals.expense_cents || 0 }

  function handleSelectAll(e) {
    if (e.target.checked) setSelected(new Set(txns.map(t => t.id)))
    else setSelected(new Set())
  }

  function handleSelect(txnId) {
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(txnId)) next.delete(txnId)
      else next.add(txnId)
      return next
    })
  }

  // Opens the picker modal; the actual write happens in applyBulkCategory.
  function handleBulkRecategorize() {
    if (selected.size === 0) return
    setBulkPicking(true)
  }

  async function applyBulkCategory(cat) {
    setBulkPicking(false)
    if (!cat) return
    setBulkMsg(null)
    try {
      const res = await fetch('/api/transactions/bulk-edit', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: [...selected], patch: { category_path: cat } }),
      })
      if (!res.ok) throw new Error('Bulk edit failed')
      setSelected(new Set())
      fetchTxns()
      setBulkMsg({ type: 'ok', text: `Recategorized ${selected.size} transactions` })
    } catch (err) {
      setBulkMsg({ type: 'err', text: err.message })
    }
  }

  async function handleRecategorize(txnId, newCat, { offerSimilar = false } = {}) {
    try {
      const res = await fetch(`/api/transactions/${txnId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ category_path: newCat }),
      })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail || `Re-categorize failed (${res.status})`)
      }
      setReclassified(r => ({ ...r, [txnId]: newCat }))
      setTxns(prev => prev.map(t => t.id === txnId ? { ...t, category_path: newCat } : t))
      if (offerSimilar) offerApplyToSimilar(txnId, newCat)
    } catch (e) {
      setBulkMsg({ type: 'err', text: e.message })
    }
  }

  // Deterministic merchant match on the server — no LLM, so this returns fast
  // enough to run on every inline edit. Silent on zero matches: an offer to
  // change nothing is just noise.
  async function offerApplyToSimilar(txnId, newCat) {
    setSimilarLoading(true)
    try {
      const d = await apiFetch(`/transactions/${txnId}/similar?category_path=${encodeURIComponent(newCat)}`)
      if (!d || !d.count || suppressedKeys.current.has(d.match_key)) return
      setSimilarPrompt({
        sourceId: txnId,
        category: newCat,
        matchKey: d.match_key,
        matchLabel: d.match_label,
        pattern: d.suggested_pattern,
        count: d.count,
        truncated: !!d.truncated,
        matches: d.matches || [],
        checked: new Set((d.matches || []).map(m => m.id)),
        applyAll: false,
        createRule: true,
        applying: false,
      })
    } catch (e) {
      // A failed suggestion must never look like a failed re-categorization —
      // the user's own edit already succeeded.
      console.error('similar lookup failed', e)
    } finally {
      setSimilarLoading(false)
    }
  }

  async function applySimilar() {
    if (!similarPrompt) return
    const p = similarPrompt
    const ids = [...p.checked]
    if (!p.applyAll && ids.length === 0) { setSimilarPrompt(null); return }
    setSimilarPrompt(s => ({ ...s, applying: true }))
    try {
      const d = await apiPost('/transactions/apply-similar', {
        source_id: p.sourceId,
        category_path: p.category,
        ids,
        all_matching: p.applyAll,
        create_rule: p.createRule,
      })
      setSimilarPrompt(null)
      fetchTxns()
      bumpDataVersion()
      let text = `Applied ${p.category} to ${d.updated_count} more ${p.matchLabel} transaction${d.updated_count === 1 ? '' : 's'}`
      if (d.rule_id) text += ' · future imports will match automatically'
      if (d.skipped_count) text += ` · ${d.skipped_count} skipped`
      setBulkMsg({ type: 'ok', text })
    } catch (e) {
      setSimilarPrompt(s => (s ? { ...s, applying: false, error: e.message } : s))
    }
  }

  function handleExportCsv() {
    const period = resolvePeriod(periodId)
    const ps = fmtDateParam(period.start)
    const pe = fmtDateParam(period.end)
    const params = new URLSearchParams({ start_date: ps, end_date: pe })
    if (searchQuery) params.set('search', searchQuery)
    if (accountFilter) params.set('account_id', accountFilter)
    if (categoryFilter) params.set('category_path', categoryFilter)
    window.open(`/api/transactions/export.csv?${params}`, '_blank')
  }

  function handleSearchKeyDown(e) {
    if (e.key === 'Enter') {
      setSearchQuery(searchInput.trim())
      setPage(1)
    }
  }

  async function handleAddSubmit(e) {
    e.preventDefault()
    setAddMsg(null)
    const amountRaw = parseInt(addForm.amount_cents, 10)
    if (isNaN(amountRaw)) { setAddMsg({ type: 'err', text: 'Amount must be a number (in cents)' }); return }
    if (!addForm.posted_date) { setAddMsg({ type: 'err', text: 'Date is required' }); return }
    try {
      const res = await fetch('/api/transactions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          amount_cents: amountRaw,
          posted_date: addForm.posted_date,
          merchant: addForm.merchant,
          category_path: addForm.category_path || null,
          business_context: addForm.business_context,
          notes: addForm.notes || null,
          account_id: addForm.account_id,
        }),
      })
      if (!res.ok) throw new Error('Create failed: ' + res.status)
      setPage(1)
      fetchTxns()
      setShowAdd(false)
      setAddForm({ merchant: '', amount_cents: '', posted_date: '', category_path: '', business_context: 'personal', notes: '', account_id: 'manual' })
      setAddMsg({ type: 'ok', text: 'Transaction added' })
    } catch (err) {
      setAddMsg({ type: 'err', text: err.message })
    }
  }

  if (loading && txns.length === 0) {
    return <div className="page"><div className="subtitle">Loading transactions...</div></div>
  }

  // Every account /api/accounts returns has transactions in the current scope,
  // so all of them belong in the filter row. This used to drop kind
  // 'real_estate'/'retirement', which was a no-op while the endpoint reported no
  // kind at all; once it did, the checking account labelled as the residence —
  // the largest account by row count — vanished from the chips.
  const bankAccts = accounts

  return (
    <div className="page">
      <div className="page-intro">
        <div className="subtitle">{total} transactions · {fmtCurrency(summary.income)} in · {fmtCurrency(summary.expense)} out</div>
        <div className="page-intro-actions">
          <button className="btn btn--ghost" onClick={handleExportCsv} title="Export as CSV">
            <Icon name="Upload" size={14}/> Export CSV
          </button>
          <button className="btn btn--primary" onClick={() => setShowAdd(true)}><Icon name="Plus" size={14}/> Add Transaction</button>
        </div>
      </div>

      {addMsg && (
        <div className={`msg-banner ${addMsg.type}`}>{addMsg.text}</div>
      )}
      {bulkMsg && (
        <div className={`msg-banner ${bulkMsg.type}`}>{bulkMsg.text}</div>
      )}

      <div className="filter-row">
        <span className="label">Refunds</span>
        {[['all','All'],['hide','Hide refunded'],['only','Only refunded']].map(([k, label]) => (
          <span key={k} className={`chip ${refundFilter === k ? 'is-active' : ''}`}
                onClick={() => { setRefundFilter(k); setPage(1) }}>{label}</span>
        ))}
        <span className="label">Account</span>
        <span className={`chip ${!accountFilter ? "is-active" : ""}`} onClick={() => { setAccountFilter(null); setPage(1) }}>All</span>
        {bankAccts.map(a => (
          <span key={a.id} className={`chip ${accountFilter === a.id ? "is-active" : ""}`}
                onClick={() => { setAccountFilter(a.id === accountFilter ? null : a.id); setPage(1) }}>
            {a.name || a.id}
          </span>
        ))}
        <div style={{flex:1}}/>
        <div style={{display:"flex", alignItems:"center", gap:6, border:"1px solid var(--input)", borderRadius:6, padding:"0 10px", height:32, background:"var(--background)"}}>
          <Icon name="Search" size={14} color="var(--muted-foreground)"/>
          <input value={searchInput} onChange={e => setSearchInput(e.target.value)} onKeyDown={handleSearchKeyDown}
                 placeholder="Search merchants…"
                 style={{border:0, outline:0, background:"transparent", color:"var(--foreground)", width:200, fontSize:13}}/>
        </div>
      </div>

      {/* Income / Expense are a question about the SIGN of the money, not about
          the category — that is the whole point of the flat model. These chips
          used to filter `category_path LIKE 'Income%'`, and once categories became
          flat names no row matched either one, so both showed an empty table. */}
      <div className="filter-row" style={{marginTop:4}}>
        <span className="label">Flow</span>
        {[['', 'All'], ['income', 'Income'], ['expense', 'Expense'], ['transfer', 'Transfer']].map(([k, label]) => (
          <span key={k || 'all'} className={`chip ${flowFilter === k ? 'is-active' : ''}`}
                title={k === 'income' ? 'Every row with a positive amount'
                     : k === 'expense' ? 'Every row with a negative amount'
                     : k === 'transfer' ? 'Money moving between your own accounts'
                     : 'No flow filter'}
                onClick={() => { setFlowFilter(k); setPage(1) }}>{label}</span>
        ))}
      </div>

      <div className="filter-row" style={{marginTop:4}}>
        <span className="label">Category</span>
        <span className={`chip ${!categoryFilter ? "is-active" : ""}`} onClick={() => { setCategoryFilter(''); setPage(1) }}>All</span>
        {catPaths.slice(0, 8).map(p => (
          <span key={p} className={`chip ${categoryFilter === p ? 'is-active' : ''}`}
                onClick={() => { setCategoryFilter(categoryFilter === p ? '' : p); setPage(1); setShowCatTree(false) }}>
            {p}
          </span>
        ))}
        <button className="btn btn--ghost btn--xs" onClick={() => setShowCatTree(!showCatTree)} style={{fontSize:12}}>
          <Icon name="Filter" size={12}/> All categories
        </button>
        {categoryFilter && (
          <button className="btn btn--ghost btn--xs" onClick={() => { setCategoryFilter(''); setPage(1); setShowCatTree(false) }} style={{marginLeft:'auto', fontSize:12}}>
            <Icon name="X" size={12}/> Clear {categoryFilter}
          </button>
        )}
      </div>

      {showCatTree && catTree.length > 0 && (
        <div className="card" style={{padding:'8px 16px', marginBottom:8, fontSize:13}}>
          <div style={{fontWeight:500, marginBottom:6, color:'var(--muted-foreground)', fontSize:11, textTransform:'uppercase', letterSpacing:'0.06em'}}>Category Tree</div>
          <CategoryTreeSelect nodes={catTree} depth={0} onSelect={(path) => { setCategoryFilter(path); setPage(1); setShowCatTree(false) }} currentPath={categoryFilter} />
        </div>
      )}

      {selected.size > 0 && (
        <div style={{display:"flex", alignItems:"center", gap:12, padding:"8px 16px", background:"color-mix(in oklab, var(--primary) 14%, transparent)", border:"1px solid color-mix(in oklab, var(--primary) 40%, transparent)", borderRadius:8}}>
          <span style={{fontSize:13, fontWeight:500}}>{selected.size} selected</span>
          <button className="btn btn--outline btn--sm" onClick={handleBulkRecategorize}>Re-categorize</button>
          <button className="btn btn--outline btn--sm" title="Keep these out of every total and chart"
                  onClick={() => applyBulkCategory('Excluded')}>Exclude</button>
          <button className="btn btn--ghost btn--sm" onClick={() => setSelected(new Set())} style={{marginLeft:'auto'}}>Clear selection</button>
        </div>
      )}

      <div className="filter-row" style={{marginTop:4, marginBottom:8, gap:8, flexWrap:'wrap'}}>
        <div style={{display:'flex', alignItems:'center', gap:4}}>
          <span className="label" style={{fontSize:11}}>Desc</span>
          <input className="form-input" style={{width:120, fontSize:12, padding:'2px 8px'}} placeholder="Description..." value={descriptionFilter} onChange={e => { setDescriptionFilter(e.target.value); setPage(1) }}/>
        </div>
        <div style={{display:'flex', alignItems:'center', gap:4}}>
          <span className="label" style={{fontSize:11}}>File</span>
          <input className="form-input" style={{width:100, fontSize:12, padding:'2px 8px'}} placeholder="Source..." value={sourceFileFilter} onChange={e => { setSourceFileFilter(e.target.value); setPage(1) }}/>
        </div>
        <div style={{display:'flex', alignItems:'center', gap:4}}>
          <span className="label" style={{fontSize:11}}>$</span>
          <input className="form-input" style={{width:70, fontSize:12, padding:'2px 8px'}} placeholder="Min" type="number" value={amountMin} onChange={e => { setAmountMin(e.target.value); setPage(1) }}/>
          <span style={{fontSize:11, color:'var(--muted-foreground)'}}>–</span>
          <input className="form-input" style={{width:70, fontSize:12, padding:'2px 8px'}} placeholder="Max" type="number" value={amountMax} onChange={e => { setAmountMax(e.target.value); setPage(1) }}/>
        </div>
        <div style={{display:'flex', alignItems:'center', gap:4}}>
          <span className="label" style={{fontSize:11}}>Flagged</span>
          <select className="form-input" style={{fontSize:12, padding:'2px 8px'}} value={flaggedFilter} onChange={e => { setFlaggedFilter(e.target.value); setPage(1) }}>
            <option value="">Any</option>
            <option value="1">Flagged</option>
            <option value="0">Unflagged</option>
          </select>
        </div>
        <div style={{display:'flex', alignItems:'center', gap:4}}>
          <label style={{fontSize:11, display:'flex', alignItems:'center', gap:4, cursor:'pointer'}}>
            <input type="checkbox" checked={showRaw} onChange={e => setShowRaw(e.target.checked)}/>
            Show raw description
          </label>
        </div>
      </div>

      <div className="card" style={{overflow:'auto'}}>
        <table className="tbl">
          <thead>
            <tr>
              <th style={{width:36}}>
                <input type="checkbox" onChange={handleSelectAll} checked={selected.size === txns.length && txns.length > 0}
                       style={{accentColor:'var(--primary)'}}/>
              </th>
              <th style={{width:80, cursor:'pointer', userSelect:'none'}} onClick={() => handleSort('posted_date')}>Date{sortIndicator('posted_date')}</th>
              <th style={{cursor:'pointer', userSelect:'none'}} onClick={() => handleSort('merchant')}>Merchant{sortIndicator('merchant')}</th>
              <th style={{cursor:'pointer', userSelect:'none'}} onClick={() => handleSort('description')}>Description{sortIndicator('description')}</th>
              <th style={{cursor:'pointer', userSelect:'none'}} onClick={() => handleSort('account_id')}>Account{sortIndicator('account_id')}</th>
              <th style={{cursor:'pointer', userSelect:'none'}} onClick={() => handleSort('category_path')}>Category{sortIndicator('category_path')}</th>
              <th className="text-right" style={{width:110, cursor:'pointer', userSelect:'none'}} onClick={() => handleSort('amount_cents')}>Amount{sortIndicator('amount_cents')}</th>
              <th style={{width:36}}></th>
            </tr>
          </thead>
          <tbody>
            {txns.map(t => {
              const isSelected = selected.has(t.id)
              const effectiveCat = reclassified[t.id] || t.category_path
              const acct = accounts.find(a => a.id === t.account_id)
              return (
                <React.Fragment key={t.id}>
                <tr className={isSelected ? 'is-selected' : ''}
                    onMouseDown={e => {
                      // Row-select must never swallow a click aimed at a control.
                      // `.no-row-select` marks the category and action cells —
                      // editing a category inline is the primary action on this
                      // table and selecting the row instead is what made it feel
                      // broken.
                      if (e.target.tagName === 'INPUT' || e.target.tagName === 'BUTTON') return
                      if (e.target.closest('.no-row-select')) return
                      handleSelect(t.id)
                    }}>
                  <td><input type="checkbox" checked={isSelected} onChange={() => handleSelect(t.id)}
                             style={{accentColor:'var(--primary)'}}/></td>
                  <td className="mono" style={{fontSize:12, color:"var(--muted-foreground)"}}>{t.posted_date?.slice(5) || ''}</td>
                  <td>
                    <div className="cell-merchant">
                      <span style={{fontWeight:500}}>{t.merchant || '—'}</span>
                      {t.flagged ? (
                        <span style={{
                          fontSize:10, padding:'1px 6px', borderRadius:999,
                          background: t.flag_reason === 'fraud' ? 'rgba(239,68,68,0.2)' : 'rgba(251,191,36,0.2)',
                          color: t.flag_reason === 'fraud' ? '#ef4444' : '#fbbf24',
                          fontWeight:600, textTransform:'uppercase', letterSpacing:0.5, whiteSpace:'nowrap',
                        }}>{t.flag_reason === 'fraud' ? 'Fraud' : t.flag_reason === 'refund-pair' ? 'Refund' : 'Flagged'}</span>
                      ) : null}
                      {t.refund_match_id ? (
                        <span className="chip" title={t.refund_role === 'credit'
                              ? 'This is the refund of an earlier purchase'
                              : (t.refund_partial ? 'Partially refunded later' : 'Fully refunded later')}
                              style={{height:18, fontSize:10, padding:'0 6px'}}>
                          <Icon name="Repeat" size={9}/>{' '}
                          {t.refund_role === 'credit' ? 'refund' : t.refund_partial ? 'part refunded' : 'refunded'}
                        </span>
                      ) : null}
                      {t.split_child_count > 0 && (
                        <span className="chip is-clickable no-row-select"
                              title={`This charge was split into ${t.split_child_count} item rows, which carry the real categories. Click to see and edit them.`}
                              onMouseDown={(e) => { e.stopPropagation(); setExpandedSplit(x => x === t.id ? null : t.id) }}
                              style={{height:18, fontSize:10, padding:'0 6px'}}>
                          <Icon name={expandedSplit === t.id ? 'ChevronDown' : 'ChevronRight'} size={9}/>{' '}
                          {t.split_child_count} items
                        </span>
                      )}
                      {reclassified[t.id] && (
                        <span className="chip" style={{height:18, fontSize:10, padding:'0 6px'}}><Icon name="Sparkles" size={9}/> reclassified</span>
                      )}
                    </div>
                  </td>
                  <td style={{fontSize:12, color:"var(--muted-foreground)", maxWidth:200, overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap"}} title={showRaw ? (t.normalized_description || '') : (t.description || '')}>
                    {showRaw ? (t.normalized_description || '—') : (t.description || '—')}
                  </td>
                  <td style={{fontSize:12.5, color:"var(--muted-foreground)"}}>{acct?.name || t.account_id || ''}</td>
                  <td className="no-row-select">
                    {editingCat === t.id ? (
                      <CategoryPicker
                        value={effectiveCat}
                        options={catPaths}
                        onPick={(path) => {
                          setEditingCat(null)
                          if (path !== effectiveCat) handleRecategorize(t.id, path, { offerSimilar: true })
                        }}
                        onCancel={() => setEditingCat(null)}
                      />
                    ) : (
                      <span className="cell-cat is-editable"
                            onMouseDown={(e) => { e.stopPropagation(); setEditingCat(t.id) }}
                            title="Click to re-categorize">
                        <span style={{fontSize:12, display:'inline-flex', alignItems:'center'}}>
                          <CategoryDot color={categoryColors[effectiveCat]}/>
                          {effectiveCat || 'Uncategorized'}
                        </span>
                        <Icon name="ChevronDown" size={11}/>
                      </span>
                    )}
                  </td>
                  <td className={`text-right mono ${t.amount_cents >= 0 ? 'amt-pos' : 'amt-neg'}`}>
                    {fmtSignedCurrency(t.amount_cents)}
                  </td>
                  <td className="no-row-select" style={{textAlign:"right", whiteSpace:'nowrap'}}>
                    {/* One-click opt-out. Setting the category to "Excluded" by
                        hand works too, but nobody would guess that. */}
                    <button className="why-btn"
                            title={effectiveCat === 'Excluded'
                              ? 'Put this back into your numbers'
                              : "Exclude from all totals and charts (e.g. you were paid back)"}
                            aria-label={effectiveCat === 'Excluded' ? 'Include transaction' : 'Exclude transaction'}
                            onClick={(e) => {
                              e.stopPropagation()
                              handleRecategorize(t.id, effectiveCat === 'Excluded' ? 'Other' : 'Excluded')
                            }}>
                      <Icon name={effectiveCat === 'Excluded' ? 'Repeat' : 'X'} size={13}/>
                    </button>
                    <button className="why-btn" title="Why this category?"
                            aria-label={`Why category for ${t.merchant || 'transaction'}`}
                            onClick={(e) => { e.stopPropagation(); setWhyTxn(t) }}>
                      <Icon name="HelpCircle" size={14}/>
                    </button>
                  </td>
                </tr>
                {/* The item rows of an expanded split, inline under their parent.
                    This is where a split charge is actually categorized — the
                    parent is only the combined total and is deliberately
                    un-editable, which is confusing until you can see the items. */}
                {expandedSplit === t.id && t.split_child_count > 0 && (
                  <SplitChildRows parent={t} catPaths={catPaths}
                                  categoryColors={categoryColors} onChanged={fetchTxns} />
                )}
                </React.Fragment>
              )
            })}
            {txns.length === 0 && (
              <tr><td colSpan={8} style={{textAlign:'center', padding:32, color:'var(--muted-foreground)'}}>No transactions found</td></tr>
            )}
          </tbody>
        </table>
      </div>

      {totalPages > 1 && (
        <div style={{display:"flex", alignItems:"center", justifyContent:"center", gap:12}}>
          <button className="btn btn--outline btn--sm" disabled={page <= 1} onClick={() => setPage(p => Math.max(1, p - 1))}>Previous</button>
          <span style={{fontSize:13, color:"var(--muted-foreground)"}}>Page {page} of {totalPages} ({total} total)</span>
          <button className="btn btn--outline btn--sm" disabled={page >= totalPages} onClick={() => setPage(p => Math.min(totalPages, p + 1))}>Next</button>
        </div>
      )}

      {whyTxn && <WhyPanel txn={whyTxn} onClose={() => setWhyTxn(null)} />}

      {similarLoading && !similarPrompt && (
        <div className="msg-banner" style={{display:'flex', alignItems:'center', gap:8}}>
          <Icon name="Loader" size={14}/> Looking for similar transactions…
        </div>
      )}

      {similarPrompt && (
        <SimilarApplyModal
          prompt={similarPrompt}
          categoryColors={categoryColors}
          onToggle={(id) => setSimilarPrompt(p => {
            const next = new Set(p.checked)
            if (next.has(id)) next.delete(id); else next.add(id)
            return { ...p, checked: next }
          })}
          onToggleAll={(on) => setSimilarPrompt(p => ({
            ...p, checked: on ? new Set(p.matches.map(m => m.id)) : new Set(),
          }))}
          onSetApplyAll={(on) => setSimilarPrompt(p => ({ ...p, applyAll: on }))}
          onSetCreateRule={(on) => setSimilarPrompt(p => ({ ...p, createRule: on }))}
          onApply={applySimilar}
          onDismiss={(suppress) => {
            if (suppress) suppressedKeys.current.add(similarPrompt.matchKey)
            setSimilarPrompt(null)
          }}
        />
      )}

      {bulkPicking && (
        <div className="modal-overlay" onClick={() => setBulkPicking(false)}>
          <div className="modal" onClick={e => e.stopPropagation()} style={{maxWidth:440}}>
            <div className="modal-header">
              <div className="modal-title">
                Categorize {selected.size} transaction{selected.size === 1 ? '' : 's'}
              </div>
              <button className="btn btn--ghost btn--sm" onClick={() => setBulkPicking(false)}>
                <Icon name="X" size={14}/>
              </button>
            </div>
            <div className="modal-body">
              <div className="form-group">
                <label className="form-label">Start typing a category</label>
                <CategoryPicker value={''} options={catPaths}
                                onPick={applyBulkCategory}
                                onCancel={() => setBulkPicking(false)} />
              </div>
              <p style={{fontSize:12, color:'var(--muted-foreground)', marginBottom:0, marginTop:74}}>
                Income or expense is decided by the amount, not the category.
              </p>
            </div>
          </div>
        </div>
      )}

      {showAdd && (
        <div className="modal-overlay" onClick={() => setShowAdd(false)}>
          <div className="modal" onClick={e => e.stopPropagation()}>
            <div className="modal-header">
              <div className="modal-title">Add Manual Transaction</div>
              <button className="btn btn--ghost btn--sm" onClick={() => setShowAdd(false)}><Icon name="X" size={14}/></button>
            </div>
            <form onSubmit={handleAddSubmit}>
              <div className="modal-body">
                <div className="form-row">
                  <div className="form-group">
                    <label className="form-label">Merchant</label>
                    <input className="form-input" value={addForm.merchant} onChange={e => setAddForm(f => ({...f, merchant: e.target.value}))} placeholder="e.g. Cash rent payment" />
                  </div>
                  <div className="form-group">
                    <label className="form-label">Amount (cents) *</label>
                    <input className="form-input" type="number" value={addForm.amount_cents} onChange={e => setAddForm(f => ({...f, amount_cents: e.target.value}))} placeholder="e.g. -150000 for $1,500 expense" />
                  </div>
                </div>
                <div className="form-row">
                  <div className="form-group">
                    <label className="form-label">Date *</label>
                    <input className="form-input" type="date" value={addForm.posted_date} onChange={e => setAddForm(f => ({...f, posted_date: e.target.value}))} />
                  </div>
                  <div className="form-group">
                    <label className="form-label">Category</label>
                    <input className="form-input" value={addForm.category_path} onChange={e => setAddForm(f => ({...f, category_path: e.target.value}))} placeholder="e.g. Income > Rentals" />
                  </div>
                </div>
                <div className="form-row">
                  <div className="form-group">
                    <label className="form-label">Account</label>
                    <select className="form-input" value={addForm.account_id} onChange={e => setAddForm(f => ({...f, account_id: e.target.value}))}>
                      <option value="manual">Manual</option>
                      <option value="cash">Cash</option>
                    </select>
                  </div>
                  <div className="form-group">
                    <label className="form-label">Business Context</label>
                    <select className="form-input" value={addForm.business_context} onChange={e => setAddForm(f => ({...f, business_context: e.target.value}))}>
                      <option value="personal">Personal</option>
                      <option value="rental">Rental</option>
                      <option value="business">Business</option>
                    </select>
                  </div>
                </div>
                <div className="form-group">
                  <label className="form-label">Notes</label>
                  <input className="form-input" value={addForm.notes} onChange={e => setAddForm(f => ({...f, notes: e.target.value}))} placeholder="Optional notes" />
                </div>
              </div>
              <div className="modal-foot">
                <button type="button" className="btn btn--outline" onClick={() => setShowAdd(false)}>Cancel</button>
                <button type="submit" className="btn btn--primary">Add Transaction</button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  )
}

// ───── Context Page ─────
function ContextPage() {
  const [content, setContent] = useState('')
  const [lastModified, setLastModified] = useState(null)
  const [saving, setSaving] = useState(false)
  const [inferring, setInferring] = useState(false)
  const [msg, setMsg] = useState(null)

  useEffect(() => {
    apiFetch('/context').then(d => {
      setContent(d.content || '')
      setLastModified(d.last_modified || null)
    }).catch(() => {})
  }, [])

  async function handleSave() {
    setSaving(true)
    setMsg(null)
    try {
      const res = await fetch('/api/context', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content }),
      })
      if (!res.ok) throw new Error('Save failed')
      const d = await res.json()
      setLastModified(d.last_modified)
      setMsg({ type: 'ok', text: 'Saved' })
    } catch (e) {
      setMsg({ type: 'err', text: 'Save failed: ' + e.message })
    } finally {
      setSaving(false)
    }
  }

  async function handleInfer() {
    setInferring(true)
    setMsg(null)
    try {
      const res = await fetch('/api/context/infer', { method: 'POST' })
      if (!res.ok) throw new Error('Infer failed')
      const d = await res.json()
      setContent(d.content || '')
      setLastModified(d.last_modified)
      setMsg({ type: 'ok', text: 'Inferred from data' })
    } catch (e) {
      setMsg({ type: 'err', text: 'Infer failed: ' + e.message })
    } finally {
      setInferring(false)
    }
  }

  return (
    <div className="page">
      <div className="page-intro">
        <div className="subtitle">
          Living knowledge base used by AI categorization{lastModified ? ' · last modified ' + new Date(lastModified).toLocaleString() : ''}
        </div>
        <div className="page-intro-actions">
          <button className="btn btn--outline btn--sm" onClick={handleInfer} disabled={inferring}>
            {inferring ? 'Inferring…' : 'Infer from data'}
          </button>
          <button className="btn btn--primary btn--sm" onClick={handleSave} disabled={saving}>
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>

      {msg && (
        <div className={`msg-banner ${msg.type}`}>
          {msg.text}
        </div>
      )}

      <div className="card">
        <div className="card-header">
          <div className="card-title">budget_context.md</div>
        </div>
        <div className="card-body" style={{ padding: 0 }}>
          <textarea
            className="ctx-textarea"
            value={content}
            onChange={e => setContent(e.target.value)}
            placeholder="Budget context will be auto-generated from your data. Click 'Infer from data' or edit manually."
            spellCheck={false}
          />
        </div>
      </div>
    </div>
  )
}

// ───── Chat Correct Page ─────
// Human-readable name for where a correction came from. "floating-widget" is an
// implementation detail that meant nothing to anyone reading their own history.
const CHAT_SOURCE_LABELS = {
  'chat-page': 'Chat page',
  'floating-widget': 'Quick chat',
}
function chatSourceLabel(source) {
  if (!source) return null
  return CHAT_SOURCE_LABELS[source] || source
}

// A chat correction can touch hundreds of rows; "affected 287" alone is not
// something anyone can trust. Expanding fetches exactly which rows changed.
function ChatAffectedRows({ correctionId, count }) {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const { categoryColors } = useContext(AppContext)

  async function toggle() {
    const next = !open
    setOpen(next)
    if (!next || data || loading) return
    setLoading(true)
    setError(null)
    try {
      setData(await apiFetch(`/chat-corrections/${correctionId}/transactions?limit=200`))
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{marginTop:6}}>
      <button className="btn btn--ghost btn--xs" onClick={toggle} style={{fontSize:11}}>
        <Icon name={open ? 'ChevronDown' : 'ChevronRight'} size={11}/>
        {' '}{count} transaction{count === 1 ? '' : 's'} changed
      </button>
      {open && (
        <div style={{marginTop:6}}>
          {loading && (
            <div className="subtitle" style={{fontSize:11, display:'flex', alignItems:'center', gap:6}}>
              <Icon name="Loader" size={12}/> Loading rows…
            </div>
          )}
          {error && <div className="msg-banner err">{error}</div>}
          {data && (
            <>
              {data.truncated && (
                <div className="subtitle" style={{fontSize:11, marginBottom:4}}>
                  Showing {data.returned} of {data.total} — most recent first.
                </div>
              )}
              <div style={{maxHeight:260, overflowY:'auto', border:'1px solid var(--border)', borderRadius:6}}>
                <table className="tbl" style={{fontSize:12}}>
                  <tbody>
                    {data.transactions.map(t => (
                      <tr key={t.id}>
                        <td className="mono" style={{width:64, whiteSpace:'nowrap', color:'var(--muted-foreground)'}}>
                          {t.posted_date?.slice(5) || ''}
                        </td>
                        <td>{t.merchant || t.description || '—'}</td>
                        <td style={{color:'var(--muted-foreground)'}}>
                          <span style={{display:'inline-flex', alignItems:'center'}}>
                            <CategoryDot color={categoryColors[t.category_path]}/>
                            {t.category_path || 'Uncategorized'}
                          </span>
                        </td>
                        <td className={`text-right mono ${t.amount_cents >= 0 ? 'amt-pos' : 'amt-neg'}`}
                            style={{width:96}}>
                          {fmtSignedCurrency(t.amount_cents)}
                        </td>
                      </tr>
                    ))}
                    {data.transactions.length === 0 && (
                      <tr><td style={{padding:12, color:'var(--muted-foreground)'}}>
                        These rows no longer exist.
                      </td></tr>
                    )}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}

function ChatPage() {
  const { bumpDataVersion } = useContext(AppContext)
  const [message, setMessage] = useState('')
  const [sending, setSending] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const [history, setHistory] = useState([])
  const [historyLoading, setHistoryLoading] = useState(true)
  // The message being processed, echoed into the history list immediately. The
  // old behaviour left the text sitting in the textarea with no sign anything
  // was happening — for a request that runs an LLM and can take 30s, that reads
  // as "the app ignored me".
  const [pending, setPending] = useState(null)
  const textRef = useRef(null)

  async function refreshHistory() {
    try {
      const d = await apiFetch('/chat-corrections?limit=50')
      setHistory(d.items || [])
    } catch {
      // ignore — show what we have
    } finally {
      setHistoryLoading(false)
    }
  }

  useEffect(() => { refreshHistory() }, [])

  async function handleSend() {
    if (!message.trim()) return
    const text = message.trim()
    setSending(true)
    setError(null)
    setResult(null)
    setPending(text)
    setMessage('')
    try {
      const data = await apiPost('/chat-correct', { message: text, source: 'chat-page' })
      setResult(data)
      // Reload server-side history so this row is now in the list
      await refreshHistory()
      // Tell other pages their data is stale
      if (data.affected_count > 0) bumpDataVersion()
    } catch (e) {
      setError(e.message)
      // Give the text back — retyping a long instruction because the request
      // failed is the worst possible outcome here.
      setMessage(text)
    } finally {
      setPending(null)
      setSending(false)
      if (textRef.current) textRef.current.focus()
    }
  }

  function handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  return (
    <div className="page">
      <div className="page-intro">
        <div className="subtitle">Use natural language to tag, re-categorize, or update rules</div>
      </div>

      {error && <div className="msg-banner err">{error}</div>}

      <div className="card">
        <div className="card-header">
          <div className="card-title">Message</div>
        </div>
        <div className="card-body">
          <textarea
            ref={textRef}
            className="chat-input"
            value={message}
            onChange={e => setMessage(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder='e.g. "tag all rent deposits with person:tenant" or "recategorize internet bills as Utilities"'
            rows={3}
            disabled={sending}
          />
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: 10, marginTop: 8 }}>
            {sending && (
              <span className="subtitle" style={{ fontSize: 12, display: 'flex', alignItems: 'center', gap: 6, marginRight: 'auto' }}>
                <Icon name="Loader" size={13}/> Working on it — reading your transactions and deciding what to change…
              </span>
            )}
            <button className="btn btn--primary" onClick={handleSend} disabled={sending || !message.trim()}>
              {sending ? <><Icon name="Loader" size={13}/> Processing…</> : 'Send'}
            </button>
          </div>
        </div>
      </div>

      {result && (
        <div className="card">
          <div className="card-header">
            <div className="card-title">Result</div>
          </div>
          <div className="card-body">
            <div className="chat-result-summary">{result.summary}</div>
            {result.affected_count > 0 && (
              <div className="chat-result-detail">
                Changed {result.affected_count} transaction{result.affected_count !== 1 ? 's' : ''}
                {' '}— expand the newest entry in History below to see exactly which.
              </div>
            )}
            {result.context_diff && (
              <div className="chat-result-diff">{result.context_diff}</div>
            )}
          </div>
        </div>
      )}

      <div className="card">
        <div className="card-header">
          <div className="card-title">History</div>
          <div className="subtitle" style={{ marginLeft: 'auto', fontSize: 11 }}>
            {historyLoading ? 'loading…' : `${history.length} entries`}
          </div>
        </div>
        <div className="card-body">
          {pending && (
            <div className="chat-history-item is-pending">
              <div className="chat-history-time">
                just now
                <span className="chat-history-badge">{chatSourceLabel('chat-page')}</span>
              </div>
              <div className="chat-history-msg">{pending}</div>
              <div className="chat-history-summary" style={{display:'flex', alignItems:'center', gap:6}}>
                <Icon name="Loader" size={13}/> Thinking…
              </div>
            </div>
          )}
          {!historyLoading && history.length === 0 && !pending && (
            <div className="subtitle">No chat corrections yet.</div>
          )}
          {history.map(h => (
            <div key={h.id} className="chat-history-item">
              <div className="chat-history-time">
                #{h.id} · {h.created_at}
                {h.source && (
                  <span className="chat-history-badge">{chatSourceLabel(h.source)}</span>
                )}
              </div>
              <div className="chat-history-msg">{h.prompt}</div>
              <div className="chat-history-summary">{h.response}</div>
              {h.context_diff && (
                <div className="chat-history-meta">{h.context_diff}</div>
              )}
              {h.affected_count > 0 && (
                <ChatAffectedRows correctionId={h.id} count={h.affected_count} />
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

// ───── Floating Chat Widget (visible on every page) ─────
function FloatingChatWidget() {
  const { bumpDataVersion } = useContext(AppContext)
  const [open, setOpen] = useState(false)
  const [message, setMessage] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState(null)
  const [history, setHistory] = useState([])
  const [pending, setPending] = useState(null)
  const bodyRef = useRef(null)

  async function refreshHistory() {
    try {
      const d = await apiFetch('/chat-corrections?limit=20')
      setHistory(d.items || [])
    } catch {
      // ignore
    }
  }

  useEffect(() => {
    if (open) refreshHistory()
  }, [open])

  // Keep the newest exchange in view — including the pending echo, which is the
  // whole point of showing it.
  useEffect(() => {
    if (bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight
  }, [history, pending])

  async function handleSend() {
    if (!message.trim()) return
    const text = message.trim()
    setSending(true)
    setError(null)
    setPending(text)
    setMessage('')
    try {
      const data = await apiPost('/chat-correct', { message: text, source: 'floating-widget' })
      await refreshHistory()
      if (data.affected_count > 0) bumpDataVersion()
    } catch (e) {
      setError(e.message)
      setMessage(text)
    } finally {
      setPending(null)
      setSending(false)
    }
  }

  function handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  return (
    <>
      <button
        className="fab-chat"
        title="Chat Correct"
        aria-label="Open chat correct widget"
        onClick={() => setOpen(o => !o)}
      >
        <Icon name="MessageSquare" size={22} />
      </button>
      {open && (
        <>
          <div className="fab-drawer-backdrop" onClick={() => setOpen(false)} />
          <div className="fab-drawer" role="dialog" aria-label="Chat Correct">
            <div className="fab-drawer-header">
              <div className="fab-drawer-title">Chat Correct</div>
              <button
                className="iconbtn"
                aria-label="Close chat widget"
                onClick={() => setOpen(false)}
              >
                <Icon name="X" size={16} />
              </button>
            </div>
            <div className="fab-drawer-body" ref={bodyRef}>
              {error && <div className="msg-banner err">{error}</div>}
              {history.length === 0 && !pending && (
                <div className="subtitle">No corrections yet. Type below to start.</div>
              )}
              {/* Oldest first so the newest exchange — and the pending echo — sit
                  at the bottom next to the input, the way every chat works. */}
              {[...history].reverse().map(h => (
                <div key={h.id} className="fab-msg">
                  <div className="fab-msg-prompt">{h.prompt}</div>
                  <div className="fab-msg-response">{h.response}</div>
                  <div className="fab-msg-meta">
                    #{h.id} · {h.created_at}
                    {h.source ? ` · ${chatSourceLabel(h.source)}` : ''}
                  </div>
                  {h.affected_count > 0 && (
                    <ChatAffectedRows correctionId={h.id} count={h.affected_count} />
                  )}
                </div>
              ))}
              {pending && (
                <div className="fab-msg is-pending">
                  <div className="fab-msg-prompt">{pending}</div>
                  <div className="fab-msg-response" style={{display:'flex', alignItems:'center', gap:6}}>
                    <Icon name="Loader" size={13}/> Thinking…
                  </div>
                </div>
              )}
            </div>
            <div className="fab-drawer-footer">
              <textarea
                className="fab-drawer-input"
                rows={2}
                value={message}
                onChange={e => setMessage(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder="Type a chat correction…"
                disabled={sending}
              />
              <div className="fab-drawer-row">
                <span className="subtitle" style={{ fontSize: 11, display: 'flex', alignItems: 'center', gap: 5 }}>
                  {sending
                    ? <><Icon name="Loader" size={12}/> Working…</>
                    : 'Enter to send · Shift+Enter for newline'}
                </span>
                <button
                  className="btn btn--primary"
                  onClick={handleSend}
                  disabled={sending || !message.trim()}
                >
                  {sending ? <><Icon name="Loader" size={13}/> Sending</> : 'Send'}
                </button>
              </div>
            </div>
          </div>
        </>
      )}
    </>
  )
}

// ───── Compare Page ─────
function ComparePage({ periodId }) {
  const { dataVersion } = useContext(AppContext)
  const [periodA, setPeriodA] = useState(periodId)
  const [periodB, setPeriodB] = useState(() => {
    const p = resolvePeriod(periodId)
    const ss = new Date(p.start.getFullYear(), p.start.getMonth() - 1, 1)
    const now = new Date()
    if (ss > now) return 'this-month'
    return periodId === 'this-month' ? 'last-month' : periodId
  })
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)

  const fetchCompare = useCallback(() => {
    setLoading(true)
    const a = resolvePeriod(periodA)
    const b = resolvePeriod(periodB)
    const aStart = fmtDateParam(a.start)
    const aEnd = fmtDateParam(a.end)
    const bStart = fmtDateParam(b.start)
    const bEnd = fmtDateParam(b.end)
    apiFetch(`/compare?period1_start=${aStart}&period1_end=${aEnd}&period2_start=${bStart}&period2_end=${bEnd}`)
      .then(d => { setData(d); setLoading(false) })
      .catch(() => setLoading(false))
  }, [periodA, periodB, dataVersion])

  useEffect(() => { fetchCompare() }, [fetchCompare])

  const pA = data?.period1 || {}
  const pB = data?.period2 || {}
  const aTot = pA.totals || { income_cents: 0, expense_cents: 0, net_cents: 0 }
  const bTot = pB.totals || { income_cents: 0, expense_cents: 0, net_cents: 0 }
  const categories = data?.categories || []

  const expRows = categories.filter(c => c.flow === 'expense')
  const incRows = categories.filter(c => c.flow === 'income')

  if (loading) {
    return (
      <div className="page">
        <div className="subtitle">Loading compare data...</div>
      </div>
    )
  }

  return (
    <div className="page">
      <div className="page-intro">
        <div className="subtitle">Side-by-side diff of any two periods</div>
      </div>

      <div className="compare-grid">
        <div>
          <div className="compare-period">
            <span className="swatch" style={{background:"var(--primary)"}}/>
            <span className="label">Period A</span>
            <span className="val mono">{resolvePeriod(periodA).label}</span>
          </div>
          <div style={{padding:"8px 0"}}>
            <PeriodPickerCompact value={periodA} onChange={setPeriodA}/>
          </div>
          <div className="card" style={{padding:"16px 20px"}}>
            <div className="kpi-label">A · Net Cash Flow</div>
            <div className={`kpi-value ${aTot.net_cents >= 0 ? "pos" : "neg"}`}>{fmtCurrency(aTot.net_cents)}</div>
            <div className="kpi-delta" style={{marginTop:8}}>
              <span style={{color:"#22c55e"}}>Income {fmtCurrency(aTot.income_cents)}</span>
              <span style={{color:"var(--destructive)"}}>Expense {fmtCurrency(aTot.expense_cents)}</span>
            </div>
          </div>
        </div>

        <div>
          <div className="compare-period" style={{background:"color-mix(in oklab, var(--accent-orange) 14%, transparent)"}}>
            <span className="swatch" style={{background:"var(--accent-orange)"}}/>
            <span className="label">Period B</span>
            <span className="val mono">{resolvePeriod(periodB).label}</span>
          </div>
          <div style={{padding:"8px 0"}}>
            <PeriodPickerCompact value={periodB} onChange={setPeriodB}/>
          </div>
          <div className="card" style={{padding:"16px 20px"}}>
            <div className="kpi-label">B · Net Cash Flow</div>
            <div className={`kpi-value ${bTot.net_cents >= 0 ? "pos" : "neg"}`}>{fmtCurrency(bTot.net_cents)}</div>
            <div className="kpi-delta" style={{marginTop:8}}>
              <span style={{color:"#22c55e"}}>Income {fmtCurrency(bTot.income_cents)}</span>
              <span style={{color:"var(--destructive)"}}>Expense {fmtCurrency(bTot.expense_cents)}</span>
            </div>
          </div>
        </div>
      </div>

      <div className="card">
        <div className="section-head">
          <div>
            <div className="h">Per-Category Diff</div>
            <div className="sub">A − B · Sorted by absolute change</div>
          </div>
          <div className="filter-row">
            <span><span className="swatch" style={{background:"var(--primary)"}}/> A: {resolvePeriod(periodA).label}</span>
            <span style={{marginLeft:8}}><span className="swatch" style={{background:"var(--accent-orange)"}}/> B: {resolvePeriod(periodB).label}</span>
          </div>
        </div>

        <div style={{padding:"6px 0 14px"}}>
          <div className="diff-row" style={{fontSize:11, color:"var(--muted-foreground)", textTransform:"uppercase", letterSpacing:"0.06em", borderBottom:"1px solid var(--border)", cursor:"default"}}>
            <span/>
            <span>Category</span>
            <span className="num">A</span>
            <span className="num">B</span>
            <span className="num">Δ</span>
            <span className="num">%</span>
          </div>
          <div style={{padding:"4px 0 0"}}>
            <div className="diff-row" style={{background:"color-mix(in oklab, var(--secondary) 50%, transparent)", cursor:"default"}}>
              <span/>
              <span style={{fontWeight:600, fontSize:11, textTransform:"uppercase", letterSpacing:"0.06em", color:"var(--muted-foreground)"}}>Expenses</span>
              <span/><span/><span/><span/>
            </div>
            {expRows.map(r => <DiffRow key={r.category_path} row={r} flow="expense"/>)}
            <div className="diff-row" style={{background:"color-mix(in oklab, var(--secondary) 50%, transparent)", cursor:"default", marginTop: 4}}>
              <span/>
              <span style={{fontWeight:600, fontSize:11, textTransform:"uppercase", letterSpacing:"0.06em", color:"var(--muted-foreground)"}}>Income</span>
              <span/><span/><span/><span/>
            </div>
            {incRows.map(r => <DiffRow key={r.category_path} row={r} flow="income"/>)}
          </div>
        </div>
      </div>
    </div>
  )
}

function DiffRow({ row, flow }) {
  const better = flow === "expense" ? row.delta_cents < 0 : row.delta_cents > 0
  const deltaClass = row.delta_cents === 0 ? "" : (better ? "down" : "up")
  const max = Math.max(row.period1_cents, row.period2_cents) || 1
  const catName = row.category_path?.split(' > ').pop() || row.category_path
  return (
    <div className="diff-row">
      <span className="swatch" style={{background: flow === "income" ? "#22c55e" : "#a78bfa"}}/>
      <div>
        <div style={{fontWeight:500}}>{catName}</div>
        <div style={{position:"relative", height:14, marginTop:4}}>
          <div style={{position:"absolute", left:0, top:0, height:5, width: (row.period1_cents / max) * 100 + "%", background:"var(--primary)", borderRadius:3}}/>
          <div style={{position:"absolute", left:0, top:8, height:5, width: (row.period2_cents / max) * 100 + "%", background:"var(--accent-orange)", borderRadius:3}}/>
        </div>
      </div>
      <span className="num mono">{fmtCurrency(row.period1_cents)}</span>
      <span className="num mono">{fmtCurrency(row.period2_cents)}</span>
      <span className={"num mono delta " + deltaClass}>{(row.delta_cents >= 0 ? "+" : "−") + fmtCurrency(Math.abs(row.delta_cents))}</span>
      <span className={"num mono delta " + deltaClass}>{row.period2_cents === 0 ? "—" : fmtPct(row.delta_pct, 0)}</span>
    </div>
  )
}

function PeriodPickerCompact({ value, onChange }) {
  return (
    <div className="seg" style={{width:"100%", flexWrap:"wrap"}}>
      {PERIOD_PRESETS.map(p => (
        <button key={p.id} className={`seg-item ${value === p.id ? "is-active" : ""}`} onClick={() => onChange(p.id)}>{p.label}</button>
      ))}
    </div>
  )
}

// ───── Trends Page ─────
function TrendsPage({ periodId }) {
  const { dataVersion } = useContext(AppContext)
  const [data, setData] = useState(null)
  const [nwData, setNwData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [mode, setMode] = useState('monthly')
  const [stackBy, setStackBy] = useState('cat')

  useEffect(() => {
    setLoading(true)
    const period = resolvePeriod(periodId)
    const ps = fmtDateParam(period.start)
    const pe = fmtDateParam(period.end)
    Promise.all([
      apiFetch(`/trends?start_date=${ps}&end_date=${pe}`),
      apiFetch('/net-worth/history'),
    ]).then(([td, nw]) => {
      setData(td)
      setNwData(nw)
      setLoading(false)
    }).catch(() => setLoading(false))
  }, [periodId, dataVersion])

  const series = useMemo(() => {
    if (!data?.series) return []
    return data.series.map(s => ({
      date: new Date(s.period + '-01'),
      income: s.income_cents,
      expense: s.expense_cents,
      cats: s.categories || {},
    }))
  }, [data])

  // Pick top 6 expense categories overall
  const topCats = useMemo(() => {
    const m = new Map()
    for (const b of series) {
      for (const [k, v] of Object.entries(b.cats)) m.set(k, (m.get(k) || 0) + v)
    }
    return [...m.entries()].sort((a, b) => b[1] - a[1]).slice(0, 6).map(([k]) => k)
  }, [series])

  const nwHistory = useMemo(() => {
    if (!nwData?.history) return []
    return nwData.history.map(p => ({ x: new Date(p.date), y: p.value }))
  }, [nwData])

  if (loading) {
    return (
      <div className="page">
        <div className="subtitle">Loading trends data...</div>
      </div>
    )
  }

  return (
    <div className="page">
      <div className="page-intro">
        <div className="subtitle">Cash flow and spend by category</div>
      </div>

      <div className="card">
        <div className="section-head">
          <div>
            <div className="h">Income vs Expense</div>
            <div className="sub">Monthly totals, net cash flow overlaid</div>
          </div>
          <div className="seg">
            {['monthly', 'quarterly'].map(m => (
              <button key={m} className={`seg-item ${mode === m ? 'is-active' : ''}`} onClick={() => setMode(m)}>
                {m[0].toUpperCase() + m.slice(1)}
              </button>
            ))}
          </div>
        </div>
        <div style={{padding:'12px 20px 20px'}}>
          <IncomeExpenseBars series={series}/>
        </div>
      </div>

      <div className="card">
        <div className="section-head">
          <div>
            <div className="h">Expense Mix Over Time</div>
            <div className="sub">Stacked spending by {stackBy === 'cat' ? 'category' : 'subcategory'}</div>
          </div>
          <div className="filter-row">
            <span className="label">Stack by</span>
            <div className="seg">
              {['cat', 'sub'].map(s => (
                <button key={s} className={`seg-item ${stackBy === s ? 'is-active' : ''}`} onClick={() => setStackBy(s)}>
                  {s === 'cat' ? 'Category' : 'Subcategory'}
                </button>
              ))}
            </div>
          </div>
        </div>
        <div style={{padding:'12px 20px 20px'}}>
          <StackedBars series={series} keys={topCats} stackBy={stackBy}/>
        </div>
      </div>

      <div className="card">
        <div className="section-head">
          <div>
            <div className="h">Net Worth Trajectory</div>
            <div className="sub">Over time</div>
          </div>
        </div>
        <div style={{padding:'12px 20px 20px'}}>
          {nwHistory.length > 0
            ? <AreaChart values={nwHistory} color="#4ade80" height={220}/>
            : <div className="sub" style={{textAlign:'center', padding:40}}>No net worth history</div>
          }
        </div>
      </div>
    </div>
  )
}

function IncomeExpenseBars({ series }) {
  const ref = useRef(null)
  const [w, setW] = useState(900)
  const [hov, setHov] = useState(null)
  useEffect(() => {
    if (!ref.current) return
    const ro = new ResizeObserver(es => { for (const e of es) setW(e.contentRect.width) })
    ro.observe(ref.current)
    return () => ro.disconnect()
  }, [])
  const height = 280
  const pad = { l: 56, r: 16, t: 12, b: 28 }
  const innerW = w - pad.l - pad.r
  const innerH = height - pad.t - pad.b
  const maxV = Math.max(...series.flatMap(s => [s.income, s.expense]), 1)
  const yMap = v => pad.t + (1 - v / maxV) * innerH
  const groupW = Math.max(innerW / series.length, 20)
  const barW = Math.max((groupW - 8) / 2, 4)

  const ticks = 4
  const yTicks = Array.from({length: ticks + 1}, (_, i) => (maxV * i) / ticks)

  return (
    <div ref={ref} style={{position:'relative'}}>
      <svg className="bar-chart" viewBox={`0 0 ${w} ${height}`} height={height}>
        {yTicks.map((t, i) => (
          <g key={i}>
            <line className="chart-grid" x1={pad.l} x2={w - pad.r} y1={yMap(t)} y2={yMap(t)}/>
            <text className="chart-label" x={pad.l - 8} y={yMap(t)} textAnchor="end" dominantBaseline="central">{fmtCurrency(t, {compact:true})}</text>
          </g>
        ))}
        {series.map((s, i) => {
          const gx = pad.l + i * groupW + 4
          const incH = Math.max(1, innerH - yMap(s.income) + pad.t)
          const expH = Math.max(1, innerH - yMap(s.expense) + pad.t)
          return (
            <g key={i} onMouseEnter={() => setHov({ i })} onMouseLeave={() => setHov(null)}>
              <rect x={gx} y={yMap(s.income)} width={barW} height={incH} fill="#22c55e" rx={2}/>
              <rect x={gx + barW + 4} y={yMap(s.expense)} width={barW} height={expH} fill="#f43f5e" rx={2}/>
              <text className="chart-label" x={gx + barW + 2} y={pad.t + innerH + 18} textAnchor="middle">{fmtMonth(s.date).split(" ")[0]}</text>
            </g>
          )
        })}
        <path
          d={series.map((s, i) => {
            const x = pad.l + i * groupW + groupW / 2
            const y = yMap(s.income - s.expense)
            return (i === 0 ? "M" : "L") + x.toFixed(1) + "," + y.toFixed(1)
          }).join(" ")}
          fill="none" stroke="#a78bfa" strokeWidth={2}/>
        {series.map((s, i) => (
          <circle key={i} cx={pad.l + i * groupW + groupW / 2} cy={yMap(s.income - s.expense)} r={3} fill="#a78bfa" stroke="var(--card)" strokeWidth={1.5}/>
        ))}
      </svg>
      <div style={{display:'flex', gap:14, fontSize:12, color:'var(--muted-foreground)', padding:'4px 0 0 56px', flexWrap:'wrap'}}>
        <span style={{whiteSpace:'nowrap'}}><span className="swatch" style={{background:'#22c55e'}}/> Income</span>
        <span style={{whiteSpace:'nowrap'}}><span className="swatch" style={{background:'#f43f5e'}}/> Expense</span>
        <span style={{whiteSpace:'nowrap'}}><span className="swatch" style={{background:'#a78bfa', borderRadius:9999}}/> Net</span>
      </div>
    </div>
  )
}

function StackedBars({ series, keys, stackBy }) {
  const ref = useRef(null)
  const [w, setW] = useState(900)
  useEffect(() => {
    if (!ref.current) return
    const ro = new ResizeObserver(es => { for (const e of es) setW(e.contentRect.width) })
    ro.observe(ref.current)
    return () => ro.disconnect()
  }, [])
  const height = 280
  const pad = { l: 56, r: 16, t: 12, b: 28 }
  const innerW = w - pad.l - pad.r
  const innerH = height - pad.t - pad.b
  const maxV = Math.max(...series.map(s => Object.values(s.cats).reduce((a, b) => a + b, 0)), 1)
  const yMap = v => pad.t + (1 - v / maxV) * innerH
  const groupW = Math.max(innerW / series.length, 20)
  const barW = Math.max(groupW - 6, 4)

  const catColors = ['#a78bfa', '#f472b6', '#34d399', '#fbbf24', '#60a5fa', '#fb923c', '#94a3b8']

  const ticks = 4
  const yTicks = Array.from({length: ticks + 1}, (_, i) => (maxV * i) / ticks)

  return (
    <div ref={ref} style={{position:'relative'}}>
      <svg className="bar-chart" viewBox={`0 0 ${w} ${height}`} height={height}>
        {yTicks.map((t, i) => (
          <g key={i}>
            <line className="chart-grid" x1={pad.l} x2={w - pad.r} y1={yMap(t)} y2={yMap(t)}/>
            <text className="chart-label" x={pad.l - 8} y={yMap(t)} textAnchor="end" dominantBaseline="central">{fmtCurrency(t, {compact:true})}</text>
          </g>
        ))}
        {series.map((s, i) => {
          const gx = pad.l + i * groupW + 3
          let yCursor = pad.t + innerH
          const cats = Object.entries(s.cats).sort((a, b) => b[1] - a[1])
          return (
            <g key={i}>
              {cats.map(([k, v], j) => {
                const h = Math.max(1, (v / maxV) * innerH)
                yCursor -= h
                const colorIdx = keys.indexOf(k)
                const color = colorIdx >= 0 ? catColors[colorIdx % catColors.length] : '#94a3b8'
                return (
                  <rect key={k} x={gx} y={yCursor} width={barW} height={h}
                        fill={color} opacity={keys.includes(k) ? 1 : 0.5} style={{cursor:'pointer'}}>
                    <title>{k.split(' > ').pop()}: {fmtCurrency(v)}</title>
                  </rect>
                )
              })}
              <text className="chart-label" x={gx + barW / 2} y={pad.t + innerH + 18} textAnchor="middle">{fmtMonth(s.date).split(" ")[0]}</text>
            </g>
          )
        })}
      </svg>
      <div style={{display:'flex', gap:14, fontSize:12, color:'var(--muted-foreground)', padding:'4px 0 0 56px', flexWrap:'wrap'}}>
        {keys.map((k, i) => (
          <span key={k} style={{cursor:'pointer', whiteSpace:'nowrap'}}>
            <span className="swatch" style={{background: catColors[i % catColors.length]}}/> {k.split(' > ').pop()}
          </span>
        ))}
      </div>
    </div>
  )
}

// ───── Net Worth Page ─────
function NetWorthPage() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    setLoading(true)
    apiFetch('/net-worth')
      .then(d => { setData(d); setLoading(false) })
      .catch(() => setLoading(false))
  }, [])

  if (loading) {
    return (
      <div className="page">
        <div className="subtitle">Loading net worth data...</div>
      </div>
    )
  }

  if (!data) {
    return (
      <div className="page">
        <div className="subtitle">No data available</div>
      </div>
    )
  }

  const accts = data.accounts || []
  const history = data.history || []
  const assets = data.assets || 0
  const liabilities = data.liabilities || 0
  const total = data.total_net_worth_cents || 0

  const groups = {
    cash: { label: 'Cash & Checking', accts: [], total: 0, color: '#38bdf8' },
    savings: { label: 'Savings', accts: [], total: 0, color: '#22d3ee' },
    brokerage: { label: 'Brokerage', accts: [], total: 0, color: '#a78bfa' },
    retirement: { label: 'Retirement', accts: [], total: 0, color: '#c4b5fd' },
    real_estate: { label: 'Real Estate', accts: [], total: 0, color: '#10b981' },
    credit: { label: 'Credit Cards', accts: [], total: 0, color: '#f43f5e' },
  }

  let computedAssets = 0, computedLiabilities = 0
  for (const a of accts) {
    const g = groups[a.kind] || groups.cash
    let val = a.balance
    if (a.kind === 'real_estate') {
      val = a.balance - (a.debt || 0)
      computedLiabilities += (a.debt || 0)
      computedAssets += a.balance
    } else if (a.kind === 'credit') {
      computedLiabilities += Math.abs(a.balance)
    } else {
      computedAssets += a.balance
    }
    g.accts.push({ ...a, displayValue: val })
    g.total += val
  }

  const nwHistory = history.length > 0 ? history : []
  const sparkValues = nwHistory.map(p => p.value)

  return (
    <div className="page">
      <div className="page-intro">
        <div className="subtitle">Snapshot across {accts.length} accounts · as of {data.as_of}</div>
      </div>

      <div className="kpi-grid" style={{gridTemplateColumns:'repeat(3, 1fr)'}}>
        <KpiCard label="Total Net Worth" value={fmtCurrency(total)}
                 valueClass={total >= 0 ? 'pos' : 'neg'} sparkValues={sparkValues} color="#4ade80"/>
        <KpiCard label="Assets" value={fmtCurrency(assets)}
                 sub={<span>Across {Object.keys(groups).filter(k => groups[k].accts.length > 0).length} categories</span>} color="#a78bfa"/>
        <KpiCard label="Liabilities" value={fmtCurrency(liabilities)}
                 sub={<span><Icon name="CircleAlert" size={12}/> {liabilities > 0 ? 'Debt across accounts' : 'No outstanding debt'}</span>}
                 color="#f43f5e"/>
      </div>

      <div className="card">
        <div className="section-head">
          <div>
            <div className="h">Net Worth Over Time</div>
            <div className="sub">{nwHistory.length} months of history</div>
          </div>
        </div>
        <div style={{padding:'12px 20px 20px'}}>
          <AreaChart values={nwHistory.map(p => ({x: new Date(p.date), y: p.value}))} color="#4ade80" height={220}/>
        </div>
      </div>

      <div className="nw-grid">
        <div className="card">
          <div className="section-head">
            <div>
              <div className="h">Allocation</div>
              <div className="sub">Where the wealth lives</div>
            </div>
          </div>
          <Treemap groups={groups} total={computedAssets}/>
        </div>

        <div className="card">
          <div className="section-head">
            <div>
              <div className="h">Accounts</div>
              <div className="sub">{accts.length} accounts across all groups</div>
            </div>
          </div>
          <div className="nw-list">
            {Object.entries(groups).map(([gid, g]) => (
              g.accts.length > 0 && (
                <React.Fragment key={gid}>
                  <div className="nw-grouptitle">
                    <div style={{display:'flex', alignItems:'center', gap:8}}>
                      <span className="swatch" style={{background: g.color}}/>
                      <span className="nw-grouptitle-name">{g.label}</span>
                    </div>
                    <div className="nw-grouptitle-amt mono">{fmtCurrency(g.total, { compact: true })}</div>
                  </div>
                  {g.accts.map(a => {
                    let h = 0; for (let i = 0; i < a.id.length; i++) h = (h * 31 + a.id.charCodeAt(i)) | 0
                    const dpct = ((h % 700) / 100) - 1.5
                    return (
                      <div className="nw-list-row" key={a.id}>
                        <span className="dot" style={{background: g.color}}/>
                        <div>
                          <div className="nw-list-name">{a.name}</div>
                          {a.debt != null
                            ? <div className="nw-list-sub">Value {fmtCurrency(a.balance, {compact:true})} · Loan {fmtCurrency(a.debt, {compact:true})}</div>
                            : <div className="nw-list-sub">{a.kind.replace('_', ' ')}</div>}
                        </div>
                        <div className="nw-list-amt mono">{fmtCurrency(a.displayValue, { compact: true })}</div>
                        <div className={`nw-list-delta ${dpct >= 0 ? 'up' : 'down'}`}>{fmtPct(dpct)}</div>
                      </div>
                    )
                  })}
                </React.Fragment>
              )
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}

function Treemap({ groups, total }) {
  const ref = useRef(null)
  const [w, setW] = useState(560)
  useEffect(() => {
    if (!ref.current) return
    const ro = new ResizeObserver(es => { for (const e of es) setW(e.contentRect.width) })
    ro.observe(ref.current)
    return () => ro.disconnect()
  }, [])
  const h = 360

  const items = useMemo(() => {
    const rows = []
    for (const [, g] of Object.entries(groups)) {
      for (const a of g.accts) {
        if (a.displayValue > 0) rows.push({ id: a.id, name: a.name, value: a.displayValue, color: g.color, group: g.label })
      }
    }
    rows.sort((a, b) => b.value - a.value)
    return rows
  }, [groups])

  const layout = useMemo(() => slice_and_dice(items, w, h), [items, w, h])

  return (
    <div ref={ref} style={{padding:'4px 20px 20px'}}>
      <svg className="nw-treemap" viewBox={`0 0 ${w} ${h}`} height={h}>
        {layout.map(r => (
          <g key={r.id}>
            <rect className="nw-tile" x={r.x} y={r.y} width={r.w} height={r.h} fill={r.color}/>
            {r.w > 70 && r.h > 30 && (
              <>
                <text className="nw-tile-label" x={r.x + 8} y={r.y + 18}>{r.name}</text>
                <text className="nw-tile-value" x={r.x + 8} y={r.y + 32}>{fmtCurrency(r.value, {compact:true})}</text>
              </>
            )}
          </g>
        ))}
      </svg>
    </div>
  )
}

function slice_and_dice(items, W, H) {
  const total = items.reduce((s, it) => s + it.value, 0)
  const out = []
  function rec(arr, x, y, w, h) {
    const sum = arr.reduce((s, it) => s + it.value, 0)
    if (arr.length === 1) { out.push({ ...arr[0], x, y, w, h }); return }
    const horizontal = w > h
    const len = horizontal ? w : h
    let acc = 0, k = 0
    for (; k < arr.length - 1; k++) {
      acc += arr[k].value
      if (acc >= sum * 0.5) { k++; break }
    }
    if (k === 0) k = 1
    const left = arr.slice(0, k), right = arr.slice(k)
    const leftSum = left.reduce((s, it) => s + it.value, 0)
    const split = (leftSum / sum) * len
    if (horizontal) {
      rec(left, x, y, split, h)
      rec(right, x + split, y, w - split, h)
    } else {
      rec(left, x, y, w, split)
      rec(right, x, y + split, w, h - split)
    }
  }
  rec(items, 0, 0, W, H)
  return out
}

function AreaChart({ values, color = 'var(--primary)', height = 200 }) {
  const ref = useRef(null)
  const [w, setW] = useState(800)
  const [hov, setHov] = useState(null)
  useEffect(() => {
    if (!ref.current) return
    const ro = new ResizeObserver(es => { for (const e of es) setW(e.contentRect.width) })
    ro.observe(ref.current)
    return () => ro.disconnect()
  }, [])
  const pad = { l: 56, r: 16, t: 12, b: 28 }
  const innerW = w - pad.l - pad.r
  const innerH = height - pad.t - pad.b
  const ys = values.map(v => v.y)
  const min = Math.min(...ys), max = Math.max(...ys)
  const yRange = max - min || 1
  const yMin = min - yRange * 0.08, yMax = max + yRange * 0.08
  const xs = values.map((_, i) => pad.l + (i / (values.length - 1)) * innerW)
  const yMap = v => pad.t + (1 - (v - yMin) / (yMax - yMin)) * innerH
  const ysPx = ys.map(yMap)
  const d = xs.map((x, i) => (i === 0 ? 'M' : 'L') + x.toFixed(1) + ',' + ysPx[i].toFixed(1)).join(' ')
  const dFill = d + ` L${xs.at(-1)},${pad.t + innerH} L${xs[0]},${pad.t + innerH} Z`
  const ticks = 4
  const yTicks = Array.from({length: ticks + 1}, (_, i) => yMin + ((yMax - yMin) * i / ticks))

  return (
    <div ref={ref} style={{position:'relative'}}>
      <svg className="area-chart" viewBox={`0 0 ${w} ${height}`} height={height}
        onMouseMove={e => {
          const rect = ref.current.querySelector('svg').getBoundingClientRect()
          const xPx = e.clientX - rect.left
          const idx = Math.max(0, Math.min(values.length - 1, Math.round((xPx - pad.l) / innerW * (values.length - 1))))
          setHov({ idx, x: xs[idx], y: ysPx[idx], v: values[idx] })
        }}
        onMouseLeave={() => setHov(null)}>
        <defs>
          <linearGradient id="areaGrad" x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity="0.35"/>
            <stop offset="100%" stopColor={color} stopOpacity="0"/>
          </linearGradient>
        </defs>
        {yTicks.map((t, i) => (
          <g key={i}>
            <line className="chart-grid" x1={pad.l} x2={w - pad.r} y1={yMap(t)} y2={yMap(t)}/>
            <text className="chart-label" x={pad.l - 8} y={yMap(t)} textAnchor="end" dominantBaseline="central">{fmtCurrency(t, {compact:true})}</text>
          </g>
        ))}
        {[0, Math.floor(values.length/4), Math.floor(values.length/2), Math.floor(values.length*3/4), values.length-1].filter(i => values[i]).map(i => (
          <text key={i} className="chart-label" x={xs[i]} y={pad.t + innerH + 18} textAnchor="middle">
            {values[i].x instanceof Date ? fmtMonth(values[i].x) : values[i].x}
          </text>
        ))}
        <path d={dFill} fill="url(#areaGrad)"/>
        <path d={d} fill="none" stroke={color} strokeWidth={2} strokeLinejoin="round"/>
        {hov && (
          <>
            <line x1={hov.x} x2={hov.x} y1={pad.t} y2={pad.t + innerH} stroke="var(--foreground)" strokeOpacity="0.3" strokeDasharray="2 3"/>
            <circle cx={hov.x} cy={hov.y} r={4} fill={color} stroke="var(--card)" strokeWidth={2}/>
          </>
        )}
      </svg>
      {hov && (
        <div className="tooltip" style={{left: hov.x + 12, top: hov.y + 12, position:'absolute'}}>
          <div className="t-label">{hov.v.x instanceof Date ? fmtMonth(hov.v.x) : hov.v.x}</div>
          <div className="t-value">{fmtCurrency(hov.v.y)}</div>
        </div>
      )}
    </div>
  )
}

// ───── Activity Page ─────

// ── Income / Expenses pages ──────────────────────────────────────────────────
// One component serves both sides: same layout, opposite flow. Includes the
// category manager, because the natural moment to fix a category is while
// looking at the spending it distorts.

const PIE_COLORS = ['#5b8def', '#4ade80', '#f59e0b', '#f43f5e', '#a78bfa', '#22d3ee',
                    '#fb923c', '#84cc16', '#e879f9', '#14b8a6', '#facc15', '#38bdf8']
// The wedge that groups the long tail. It is a real, hoverable slice — it just
// stands for several categories, so it gets a neutral colour of its own.
const OTHER_BUCKET_COLOR = '#94a3b8'

// A small coloured square, used wherever a category name appears so the chart
// legend and the transactions list agree at a glance.
function CategoryDot({ color, size = 9 }) {
  return (
    <span aria-hidden="true" style={{
      display:'inline-block', width:size, height:size, borderRadius:2, marginRight:6,
      background: color || 'var(--muted-foreground)', flex:'0 0 auto',
    }}/>
  )
}

function DonutChart({ slices, total, size = 230, onHover }) {
  // Hand-rolled SVG: this app carries no charting dependency and the rest of the
  // UI (Sparkline, Sankey) is drawn the same way.
  const [hover, setHover] = useState(null)
  const radius = size / 2
  const thickness = size * 0.24
  const inner = radius - thickness
  let angle = -Math.PI / 2

  const arcs = slices.map((s, i) => {
    const frac = total > 0 ? s.value / total : 0
    const sweep = frac * Math.PI * 2
    // Grow the hovered slice outward so the selection is unmistakable.
    const isHot = hover === i
    const r = isHot ? radius : radius - 5
    const ri = isHot ? inner - 2 : inner
    const x1 = radius + r * Math.cos(angle), y1 = radius + r * Math.sin(angle)
    const x2 = radius + r * Math.cos(angle + sweep), y2 = radius + r * Math.sin(angle + sweep)
    const xi2 = radius + ri * Math.cos(angle + sweep), yi2 = radius + ri * Math.sin(angle + sweep)
    const xi1 = radius + ri * Math.cos(angle), yi1 = radius + ri * Math.sin(angle)
    const large = sweep > Math.PI ? 1 : 0
    const d = `M ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2} L ${xi2} ${yi2} A ${ri} ${ri} 0 ${large} 0 ${xi1} ${yi1} Z`
    angle += sweep
    return { d, color: s.color || PIE_COLORS[i % PIE_COLORS.length], label: s.label, value: s.value, frac, isHot }
  })

  const active = hover != null ? arcs[hover] : null

  return (
    <div style={{position:'relative', width:size, height:size}}
         onMouseLeave={() => { setHover(null); onHover?.(null, null) }}>
      <svg width={size} height={size} role="img" aria-label="Category breakdown">
        {arcs.map((a, i) => (
          <path key={i} d={a.d} fill={a.color}
                stroke="var(--background)" strokeWidth={a.isHot ? 2 : 1}
                opacity={hover == null || a.isHot ? 1 : 0.35}
                style={{cursor:'pointer', transition:'opacity .12s'}}
                onMouseEnter={() => { setHover(i); onHover?.(a.label, slices[i]) }} />
        ))}
        {/* Centre reads out whatever is hovered, else the total. */}
        <text x={radius} y={radius - 6} textAnchor="middle"
              style={{fontSize:11, fill:'var(--muted-foreground)'}}>
          {active ? String(active.label).slice(0, 20) : 'Total'}
        </text>
        <text x={radius} y={radius + 13} textAnchor="middle"
              style={{fontSize:17, fontWeight:700, fill:'var(--foreground)'}}>
          {fmtCurrency(active ? active.value : total)}
        </text>
        {active && (
          <text x={radius} y={radius + 30} textAnchor="middle"
                style={{fontSize:12, fill:'var(--muted-foreground)'}}>
            {(active.frac * 100).toFixed(1)}%
          </text>
        )}
      </svg>
      {active && (
        <div style={{position:'absolute', left:'50%', top:-6, transform:'translate(-50%,-100%)',
                     background:'var(--card)', border:'1px solid var(--border)', borderRadius:6,
                     padding:'6px 10px', fontSize:12, whiteSpace:'nowrap', pointerEvents:'none',
                     boxShadow:'0 6px 18px rgba(0,0,0,.35)', zIndex:5}}>
          <b>{active.label}</b> · {fmtCurrency(active.value)} · {(active.frac * 100).toFixed(1)}%
        </div>
      )}
    </div>
  )
}

function CategoryManager({ onChanged }) {
  // Flat model: one list, one name each. No parents, no flow selector — whether a
  // category is income or spending is decided by the sign of the money in it.
  const [cats, setCats] = useState([])
  const [usage, setUsage] = useState({})
  const [newName, setNewName] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)
  const [editing, setEditing] = useState(null)
  const [editName, setEditName] = useState('')
  const [confirming, setConfirming] = useState(null)
  const [moveTo, setMoveTo] = useState('')
  const { categoryColors, palette, reloadColors } = useContext(AppContext)
  const [coloring, setColoring] = useState(null)

  async function setColor(path, color) {
    setColoring(null)
    try {
      const all = await apiFetch('/categories')
      const cat = (all.categories || []).find(c => c.path === path)
      await apiFetch(`/categories/${cat.id}/color`, {
        method: 'PATCH', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ color }),
      })
      reloadColors?.()
      onChanged?.()
      setMsg({ ok: true, text: `Colour updated for “${path}”` })
    } catch (e) { setMsg({ ok: false, text: String(e.message || e) }) }
  }

  const load = useCallback(() => {
    apiFetch('/categories').then(d => setCats((d.categories || []).map(c => c.path).sort())).catch(() => {})
    apiFetch('/dashboard?scope=plaid')
      .then(d => {
        const u = {}
        for (const c of d.by_category || []) u[c.category_path] = c.transaction_count
        setUsage(u)
      }).catch(() => {})
  }, [])
  useEffect(() => { load() }, [load])

  async function add() {
    const name = newName.trim()
    if (!name) return
    setBusy(true); setMsg(null)
    try {
      await apiPost('/categories', { name, parent_path: null })
      setNewName(''); load(); onChanged?.()
      setMsg({ ok: true, text: `Added “${name}”` })
    } catch (e) { setMsg({ ok: false, text: String(e.message || e) }) }
    setBusy(false)
  }

  async function rename(oldPath) {
    const name = editName.trim()
    if (!name) return
    setBusy(true); setMsg(null)
    try {
      const all = await apiFetch('/categories')
      const cat = (all.categories || []).find(c => c.path === oldPath)
      await apiFetch(`/categories/${cat.id}`, {
        method: 'PUT', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ name }),
      })
      setEditing(null); load(); onChanged?.()
      setMsg({ ok: true, text: `Renamed to “${name}”` })
    } catch (e) { setMsg({ ok: false, text: String(e.message || e) }) }
    setBusy(false)
  }

  async function remove(path) {
    setBusy(true); setMsg(null)
    try {
      const all = await apiFetch('/categories')
      const cat = (all.categories || []).find(c => c.path === path)
      const params = new URLSearchParams()
      if (moveTo) params.set('reassign_to', moveTo)
      else params.set('force', 'true')
      const r = await apiFetch(`/categories/${cat.id}?${params}`, { method: 'DELETE' })
      setConfirming(null); setMoveTo(''); load(); onChanged?.()
      setMsg({ ok: true, text: `Deleted “${path}”${r.transactions_moved ? ` — ${r.transactions_moved} transactions moved to ${r.moved_to}` : ''}` })
    } catch (e) { setMsg({ ok: false, text: String(e.message || e) }) }
    setBusy(false)
  }

  return (
    <div className="card">
      <div className="section-head">
        <div>
          <div className="h">Categories</div>
          <div className="sub">{cats.length} categories · flat, no hierarchy · income vs expense follows the amount</div>
        </div>
      </div>

      <div style={{display:'flex', gap:8, padding:'0 14px 12px', flexWrap:'wrap'}}>
        <input className="form-input" style={{maxWidth:260, height:32}} value={newName}
               placeholder="New category name" onChange={e => setNewName(e.target.value)}
               onKeyDown={e => { if (e.key === 'Enter') add() }} />
        <button className="btn btn--primary" onClick={add} disabled={busy || !newName.trim()}>
          <Icon name="Plus" size={14}/> Add
        </button>
      </div>

      {msg && (
        <div style={{padding:'0 14px 10px', fontSize:12.5,
                     color: msg.ok ? 'var(--muted-foreground)' : 'var(--destructive)'}}>
          {msg.text}
        </div>
      )}

      <div style={{display:'flex', flexWrap:'wrap', gap:8, padding:'0 14px 16px'}}>
        {cats.map(path => (
          <div key={path} style={{display:'flex', alignItems:'center', gap:6, position:'relative',
                                  border:'1px solid var(--border)', borderRadius:20,
                                  padding:'5px 6px 5px 12px', background:'var(--card)'}}>
            {editing === path ? (
              <>
                <input className="form-input" style={{height:26, fontSize:12, width:150}}
                       value={editName} autoFocus
                       onChange={e => setEditName(e.target.value)}
                       onKeyDown={e => { if (e.key === 'Enter') rename(path); if (e.key === 'Escape') setEditing(null) }} />
                <button className="btn btn--primary btn--sm" onClick={() => rename(path)}>Save</button>
                <button className="btn btn--ghost btn--sm" onClick={() => setEditing(null)}>✕</button>
              </>
            ) : (
              <>
                <button className="why-btn" title="Change colour"
                        onClick={() => setColoring(coloring === path ? null : path)}
                        style={{padding:0, width:14, height:14}}>
                  <CategoryDot color={categoryColors[path]} size={12}/>
                </button>
                <span style={{fontSize:13}}>{path}</span>
                <span style={{fontSize:11, color:'var(--muted-foreground)'}}>
                  {usage[path] ? `${usage[path]}` : '0'}
                </span>
                <button className="why-btn" title="Rename"
                        onClick={() => { setEditing(path); setEditName(path) }}>
                  <Icon name="Tag" size={12}/>
                </button>
                <button className="why-btn" title="Delete"
                        onClick={() => { setConfirming(path); setMoveTo('') }}>
                  <Icon name="X" size={12}/>
                </button>
              </>
            )}
            {coloring === path && (
              <div style={{position:'absolute', zIndex:40, marginTop:30, display:'flex',
                           flexWrap:'wrap', gap:4, width:150, padding:6,
                           background:'var(--card)', border:'1px solid var(--border)',
                           borderRadius:6, boxShadow:'0 6px 18px rgba(0,0,0,.35)'}}>
                {(palette || []).map(c => (
                  <button key={c} title={c} onClick={() => setColor(path, c)}
                          style={{width:18, height:18, borderRadius:4, background:c,
                                  border: categoryColors[path] === c ? '2px solid var(--foreground)' : '1px solid var(--border)',
                                  cursor:'pointer'}}/>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>

      {confirming && (
        <div className="modal-overlay" onClick={() => setConfirming(null)}>
          <div className="modal" onClick={e => e.stopPropagation()} style={{maxWidth:460}}>
            <div className="modal-header">
              <div className="modal-title">Delete “{confirming}”</div>
              <button className="btn btn--ghost btn--sm" onClick={() => setConfirming(null)}>
                <Icon name="X" size={14}/>
              </button>
            </div>
            <div className="modal-body">
              <p style={{marginTop:0, fontSize:13, color:'var(--muted-foreground)'}}>
                {usage[confirming]
                  ? `${usage[confirming]} transactions use this category. Move them somewhere, or they become Uncategorized.`
                  : 'Nothing uses this category, so it can be removed cleanly.'}
              </p>
              <div className="form-group">
                <label className="form-label">Move its transactions to</label>
                <select className="form-input" value={moveTo} onChange={e => setMoveTo(e.target.value)}>
                  <option value="">Leave them Uncategorized</option>
                  {cats.filter(c => c !== confirming).map(c => <option key={c} value={c}>{c}</option>)}
                </select>
              </div>
            </div>
            <div className="modal-foot">
              <button className="btn btn--outline" onClick={() => setConfirming(null)}>Cancel</button>
              <button className="btn btn--primary" disabled={busy} onClick={() => remove(confirming)}>
                Delete category
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// The transactions behind one category total, biggest first. Clicking a row in
// the breakdown is the natural next question — "what IS my $1,240 of Groceries?"
// — and until now the answer meant leaving for the Transactions tab and rebuilding
// the same filter by hand.
function CategoryDrillDown({ category, isIncome, periodId, expectedCents, onClose }) {
  const { categoryColors } = useContext(AppContext)
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [limit, setLimit] = useState(100)

  useEffect(() => {
    let live = true
    setLoading(true)
    setError(null)
    const period = resolvePeriod(periodId)
    const qs = new URLSearchParams({
      path: category,
      start_date: fmtDateParam(period.start),
      end_date: fmtDateParam(period.end),
      // Magnitude, not signed value: expenses are negative, so a plain descending
      // sort would show the smallest one first.
      sort: 'abs_amount',
      dir: 'desc',
      page_size: String(limit),
    })
    apiFetch(`/dashboard/category-detail?${qs}`)
      .then(d => { if (live) { setData(d); setLoading(false) } })
      .catch(e => { if (live) { setError(e.message); setLoading(false) } })
    return () => { live = false }
  }, [category, periodId, limit])

  const shown = data?.transactions || []
  // The drill-down selects exactly the rows the breakdown counted, so these two
  // numbers must agree. If they ever don't, say so rather than hide it.
  const netAbs = Math.abs(data?.net_cents ?? 0)
  const mismatch = data && expectedCents != null && Math.abs(netAbs - expectedCents) > 0

  return (
    <div className="card" style={{marginTop:12}}>
      <div className="section-head">
        <div>
          <div className="h" style={{display:'flex', alignItems:'center'}}>
            <CategoryDot color={categoryColors[category]}/>{category}
          </div>
          <div className="sub">
            {loading ? 'Loading…' : `${data?.total ?? 0} transaction${data?.total === 1 ? '' : 's'} · ${fmtCurrency(netAbs)} net · largest first`}
            {data?.scope ? ` · ${data.scope} data` : ''}
          </div>
        </div>
        <button className="btn btn--ghost btn--sm" onClick={onClose} style={{marginLeft:'auto'}}>
          <Icon name="X" size={14}/> Close
        </button>
      </div>

      {error && <div className="msg-banner err">{error}</div>}
      {mismatch && (
        <div className="msg-banner err" style={{margin:'0 14px 8px'}}>
          These rows net {fmtCurrency(netAbs)} but the breakdown says {fmtCurrency(expectedCents)} — that
          gap is a bug, not a rounding artefact.
        </div>
      )}

      <div style={{overflow:'auto', maxHeight:460}}>
        <table className="tbl">
          <thead><tr>
            <th style={{width:80}}>Date</th>
            <th>Merchant</th>
            <th>Description</th>
            <th>Account</th>
            <th className="text-right" style={{width:110}}>Amount</th>
          </tr></thead>
          <tbody>
            {shown.map(t => (
              <tr key={t.id}>
                <td className="mono" style={{width:80, whiteSpace:'nowrap', fontSize:12, color:'var(--muted-foreground)'}}>
                  {t.posted_date || ''}
                </td>
                <td style={{fontWeight:500}}>{t.merchant || '—'}</td>
                <td style={{fontSize:12, color:'var(--muted-foreground)', maxWidth:260,
                            overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap'}}
                    title={t.description || ''}>
                  {t.description || '—'}
                </td>
                <td style={{fontSize:12, color:'var(--muted-foreground)'}}>{t.account_id || ''}</td>
                <td className={`text-right mono ${t.amount_cents >= 0 ? 'amt-pos' : 'amt-neg'}`}>
                  {fmtSignedCurrency(t.amount_cents)}
                </td>
              </tr>
            ))}
            {!loading && shown.length === 0 && (
              <tr><td colSpan={5} style={{textAlign:'center', padding:24, color:'var(--muted-foreground)'}}>
                No {isIncome ? 'income' : 'expenses'} in this category for this period.
              </td></tr>
            )}
          </tbody>
        </table>
      </div>

      {data && data.total > shown.length && (
        <div style={{padding:'10px 14px', textAlign:'center'}}>
          <button className="btn btn--outline btn--sm" onClick={() => setLimit(l => l + 200)}>
            Show more ({shown.length} of {data.total})
          </button>
        </div>
      )}
    </div>
  )
}

function FlowPage({ flow, periodId }) {
  const { dataVersion } = useContext(AppContext)
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [version, setVersion] = useState(0)

  useEffect(() => {
    setLoading(true)
    const period = resolvePeriod(periodId)
    const qs = `start_date=${fmtDateParam(period.start)}&end_date=${fmtDateParam(period.end)}`
    apiFetch(`/dashboard?${qs}`)
      .then(d => { setData(d); setLoading(false) })
      .catch(() => setLoading(false))
  }, [periodId, dataVersion, version])

  const [hovered, setHovered] = useState(null)
  const [hoveredMembers, setHoveredMembers] = useState(null)
  const [drill, setDrill] = useState(null)
  const { categoryColors } = useContext(AppContext)
  const isIncome = flow === 'income'
  const rows = useMemo(() => {
    const cats = data?.by_category || []
    return cats
      .map(c => ({ label: c.category_path, value: isIncome ? c.income_cents : c.expense_cents }))
      .filter(r => r.value > 0)
      .sort((a, b) => b.value - a.value)
  }, [data, isIncome])

  const total = rows.reduce((s, r) => s + r.value, 0)
  // Keep the donut readable: top 11 plus a grouped tail. The tail is labelled
  // with what it contains so hovering it is meaningful rather than a dead end.
  const tail = rows.slice(11)
  const slices = (rows.length <= 12 ? rows : [
    ...rows.slice(0, 11).map(r => ({...r, color: categoryColors[r.label]})),
    {
      label: `Other (${tail.length} categories)`,
      value: tail.reduce((s, r) => s + r.value, 0),
      color: OTHER_BUCKET_COLOR,
      members: tail.map(r => r.label),
    },
  ]).map(r => ({...r, color: r.color || categoryColors[r.label]}))

  return (
    <div className="page">
      <div className="page-intro">
        <div className="subtitle">
          {isIncome ? 'Money in' : 'Money out'} by category ·{' '}
          {data?.scope ? `${data.scope} data` : ''} · {rows.length} categories
        </div>
      </div>

      {loading ? <div className="card" style={{padding:20}}>Loading…</div> : total === 0 ? (
        <div className="card" style={{padding:20}}>
          No {isIncome ? 'income' : 'expenses'} in this period.
        </div>
      ) : (
        <div className="card">
          <div style={{display:'flex', gap:26, flexWrap:'wrap', alignItems:'flex-start', padding:16}}>
            <DonutChart slices={slices} total={total}
                        onHover={(label, slice) => { setHovered(label); setHoveredMembers(slice?.members || null) }}/>
            <div style={{flex:1, minWidth:300}}>
              <table className="tbl">
                <thead><tr>
                  <th>Category</th><th className="text-right">Amount</th><th className="text-right">Share</th>
                </tr></thead>
                <tbody>
                  {rows.map((r, i) => {
                    const isOpen = drill?.category === r.label
                    return (
                    <tr key={r.label}
                        title={`Show the ${isIncome ? 'income' : 'expenses'} behind ${r.label}`}
                        onClick={() => setDrill(isOpen ? null : { category: r.label, value: r.value })}
                        style={{background: isOpen
                          ? 'color-mix(in oklab, var(--primary) 14%, transparent)'
                          : (hovered === r.label || (hoveredMembers || []).includes(r.label))
                          ? 'var(--accent, rgba(255,255,255,.06))' : 'transparent'}}>
                      <td style={{fontSize:12.5}}>
                        <CategoryDot color={categoryColors[r.label]}/>
                        {r.label}
                        <Icon name={isOpen ? 'ChevronDown' : 'ChevronRight'} size={11}/>
                      </td>
                      <td className="text-right mono">{fmtCurrency(r.value)}</td>
                      <td className="text-right mono" style={{color:'var(--muted-foreground)'}}>
                        {((r.value / total) * 100).toFixed(1)}%
                      </td>
                    </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {drill && (
        <CategoryDrillDown category={drill.category} isIncome={isIncome} periodId={periodId}
                           expectedCents={drill.value} onClose={() => setDrill(null)}/>
      )}

      <CategoryManager onChanged={() => setVersion(v => v + 1)}/>
    </div>
  )
}


// ── Excluded ─────────────────────────────────────────────────────────────────
// Rows the owner has opted out of every total. They must remain VISIBLE: an
// amount that vanishes with no way to inspect it is indistinguishable from a bug.

function ExcludedPage({ periodId }) {
  const { dataVersion, bumpDataVersion, categoryColors } = useContext(AppContext)
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [catPaths, setCatPaths] = useState([])
  const [restoring, setRestoring] = useState(null)
  const [version, setVersion] = useState(0)

  useEffect(() => {
    setLoading(true)
    const period = resolvePeriod(periodId)
    const qs = `start_date=${fmtDateParam(period.start)}&end_date=${fmtDateParam(period.end)}`
    apiFetch(`/excluded?${qs}`)
      .then(d => { setData(d); setLoading(false) })
      .catch(() => setLoading(false))
    apiFetch('/categories')
      .then(d => setCatPaths((d.categories || []).map(c => c.path).filter(p => p !== 'Excluded').sort()))
      .catch(() => {})
  }, [periodId, dataVersion, version])

  async function putBack(txnId, category) {
    setRestoring(null)
    try {
      await apiFetch(`/transactions/${txnId}`, {
        method: 'PATCH', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ category_path: category }),
      })
      setVersion(v => v + 1)
      bumpDataVersion?.()
    } catch (e) { /* surfaced by the reload showing it still excluded */ }
  }

  const t = data?.totals || {}
  const rows = data?.transactions || []

  return (
    <div className="page">
      <div className="page-intro">
        <div className="subtitle">
          Not counted anywhere — no charts, no totals, no budgets. Use this for
          one-offs you fronted and were paid back for.
        </div>
      </div>

      <div className="kpi-grid" style={{gridTemplateColumns:'repeat(3, 1fr)'}}>
        <KpiCard label="Excluded transactions" value={String(t.transaction_count ?? 0)}
                 sub={<span>hidden from every total</span>} color="#94a3b8"/>
        <KpiCard label="Excluded spending" value={fmtCurrency(t.excluded_outflow_cents || 0)}
                 sub={<span>would have counted as expense</span>} color="#f43f5e"/>
        <KpiCard label="Excluded money in" value={fmtCurrency(t.excluded_inflow_cents || 0)}
                 sub={<span>would have counted as income</span>} color="#4ade80"/>
      </div>

      <div className="card">
        <div className="section-head">
          <div>
            <div className="h">Excluded transactions</div>
            <div className="sub">
              {rows.length === 0
                ? 'Nothing excluded in this period. Set a transaction’s category to “Excluded” to keep it out of your numbers.'
                : `${rows.length} shown — pick a category to put one back`}
            </div>
          </div>
        </div>
        {rows.length > 0 && (
          <div style={{overflow:'auto'}}>
            <table className="tbl">
              <thead><tr>
                <th>Date</th><th>Merchant</th><th>Account</th>
                <th className="text-right">Amount</th><th style={{width:250}}>Put back into</th>
              </tr></thead>
              <tbody>
                {rows.map(r => (
                  <tr key={r.id}>
                    <td className="mono" style={{fontSize:12}}>{r.posted_date}</td>
                    <td style={{fontSize:12.5}}>{(r.merchant || r.description || '').slice(0, 46)}</td>
                    <td style={{fontSize:12, color:'var(--muted-foreground)'}}>{r.account_id}</td>
                    <td className={`text-right mono ${r.amount_cents >= 0 ? 'amt-pos' : 'amt-neg'}`}>
                      {fmtCurrency(r.amount_cents)}
                    </td>
                    <td>
                      {restoring === r.id ? (
                        <CategoryPicker value={''} options={catPaths}
                                        onPick={(cat) => putBack(r.id, cat)}
                                        onCancel={() => setRestoring(null)}/>
                      ) : (
                        <button className="btn btn--ghost btn--sm" onClick={() => setRestoring(r.id)}>
                          <Icon name="Repeat" size={12}/> Put back
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}


// ── Refunded ─────────────────────────────────────────────────────────────────
// Purchases paired with the credit that reversed them. Totals are unaffected —
// a refunded purchase already nets to zero in its category — so this tab is about
// seeing the washes, and the Transactions filter is about hiding them.

function RefundedPage({ periodId }) {
  const { dataVersion, bumpDataVersion } = useContext(AppContext)
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [detecting, setDetecting] = useState(false)
  const [msg, setMsg] = useState(null)
  const [filter, setFilter] = useState('all')
  const [version, setVersion] = useState(0)
  const [catPaths, setCatPaths] = useState([])
  const [editing, setEditing] = useState(null)

  useEffect(() => {
    apiFetch('/categories').then(d => setCatPaths((d.categories || []).map(c => c.path).sort())).catch(() => {})
  }, [])

  // Recategorize BOTH halves: a charge and its refund belong in the same bucket,
  // and fixing only one leaves the pair contradicting itself.
  async function recategorizePair(pair, category) {
    setEditing(null)
    try {
      await apiPost('/transactions/bulk-edit', {
        ids: [pair.charge_id, pair.credit_id], patch: { category_path: category },
      })
      setMsg({ ok: true, text: `Moved both halves to “${category}”` })
      setVersion(v => v + 1)
      bumpDataVersion?.()
    } catch (e) { setMsg({ ok: false, text: String(e.message || e) }) }
  }

  // Every refunded pair from one merchant, in one go — the disputed-merchant case.
  async function recategorizeMerchant(pair, category) {
    setEditing(null)
    const key = (pair.charge_merchant || pair.credit_merchant || '').toLowerCase().replace(/[^a-z0-9]/g, '')
    const ids = []
    for (const p of (data?.pairs || [])) {
      const k = (p.charge_merchant || p.credit_merchant || '').toLowerCase().replace(/[^a-z0-9]/g, '')
      if (k === key) ids.push(p.charge_id, p.credit_id)
    }
    try {
      await apiPost('/transactions/bulk-edit', { ids, patch: { category_path: category } })
      setMsg({ ok: true, text: `Moved ${ids.length} transactions from this merchant to “${category}”` })
      setVersion(v => v + 1)
      bumpDataVersion?.()
    } catch (e) { setMsg({ ok: false, text: String(e.message || e) }) }
  }

  useEffect(() => {
    setLoading(true)
    const period = resolvePeriod(periodId)
    const qs = `start_date=${fmtDateParam(period.start)}&end_date=${fmtDateParam(period.end)}`
    apiFetch(`/refunded?${qs}`)
      .then(d => { setData(d); setLoading(false) })
      .catch(() => setLoading(false))
  }, [periodId, dataVersion, version])

  async function rescan() {
    setDetecting(true); setMsg(null)
    try {
      const r = await apiPost('/refunds/detect', {})
      setMsg({ ok: true, text: `${r.pairs} refunded purchases found — ${r.full_refunds} full, ${r.partial_refunds} partial` })
      setVersion(v => v + 1)
    } catch (e) { setMsg({ ok: false, text: String(e.message || e) }) }
    setDetecting(false)
  }

  const t = data?.totals || {}
  const all = data?.pairs || []
  const pairs = filter === 'full' ? all.filter(p => !p.refund_partial)
    : filter === 'partial' ? all.filter(p => p.refund_partial) : all

  return (
    <div className="page">
      <div className="page-intro">
        <div className="subtitle">
          Purchases matched to the credit that reversed them. Your totals already
          net these out — hide them from the list with “Hide refunded” on Transactions.
        </div>
        <button className="btn btn--ghost" onClick={rescan} disabled={detecting}>
          <Icon name="Repeat" size={14}/> {detecting ? 'Scanning…' : 'Re-scan'}
        </button>
      </div>

      {msg && (
        <div className="card" style={{padding:12, marginBottom:12,
             color: msg.ok ? 'var(--muted-foreground)' : 'var(--destructive)'}}>{msg.text}</div>
      )}

      <div className="kpi-grid" style={{gridTemplateColumns:'repeat(4, 1fr)'}}>
        <KpiCard label="Refunded purchases" value={String(all.length)}
                 sub={<span>{t.partial_count || 0} partial</span>} color="#a78bfa"/>
        <KpiCard label="Money back" value={fmtCurrency(t.refunded_cents || 0)}
                 sub={<span>credited to your cards</span>} color="#4ade80"/>
        <KpiCard label="Original spend" value={fmtCurrency(t.original_spend_cents || 0)}
                 sub={<span>before the refunds</span>} color="#94a3b8"/>
        <KpiCard label="Kept after refunds" value={fmtCurrency(t.still_outstanding_cents || 0)}
                 sub={<span>partial refunds only</span>} color="#f59e0b"/>
      </div>

      <div className="filter-row">
        <span className="label">Show</span>
        {[['all','All'],['full','Fully refunded'],['partial','Partial']].map(([k,label]) => (
          <span key={k} className={`chip ${filter === k ? 'is-active' : ''}`}
                onClick={() => setFilter(k)}>{label}</span>
        ))}
      </div>

      <div className="card">
        <div className="section-head">
          <div>
            <div className="h">Refunded purchases</div>
            <div className="sub">
              {loading ? 'Scanning…' : pairs.length === 0
                ? 'Nothing refunded in this period. Hit Re-scan if you have just synced.'
                : `${pairs.length} shown, newest refund first`}
            </div>
          </div>
        </div>
        {pairs.length > 0 && (
          <div style={{overflow:'auto'}}>
            <table className="tbl">
              <thead><tr>
                <th>Merchant</th><th>Category</th><th>Bought</th>
                <th className="text-right">Charge</th><th>Refunded</th>
                <th className="text-right">Back</th><th className="text-right">Kept</th>
                <th style={{width:250}}>Category / actions</th>
              </tr></thead>
              <tbody>
                {pairs.map(p => (
                  <tr key={`${p.charge_id}-${p.credit_id}`}>
                    <td style={{fontSize:12.5}}>{(p.charge_merchant || p.credit_merchant || '').slice(0, 30)}</td>
                    <td style={{fontSize:12, color:'var(--muted-foreground)'}}>{p.category_path || '—'}</td>
                    <td className="mono" style={{fontSize:12}}>{p.charge_date}</td>
                    <td className="text-right mono amt-neg">{fmtCurrency(p.charge_cents)}</td>
                    <td className="mono" style={{fontSize:12}}>
                      {p.credit_date}
                      <span style={{color:'var(--muted-foreground)'}}> ({p.days_to_refund}d)</span>
                    </td>
                    <td className="text-right mono amt-pos">{fmtCurrency(p.credit_cents)}</td>
                    <td className="text-right mono">
                      {p.outstanding_cents ? fmtCurrency(-p.outstanding_cents) : '—'}
                    </td>
                    <td>
                      {editing === p.credit_id ? (
                        <div>
                          <CategoryPicker value={p.category_path || ''} options={catPaths}
                                          onPick={(cat) => recategorizePair(p, cat)}
                                          onCancel={() => setEditing(null)}/>
                        </div>
                      ) : (
                        <div style={{display:'flex', gap:6, alignItems:'center', flexWrap:'wrap'}}>
                          <span className="chip" style={{fontSize:10}}>
                            {p.refund_partial ? 'partial' : 'full'}
                          </span>
                          <button className="btn btn--ghost btn--sm" onClick={() => setEditing(p.credit_id)}>
                            <Icon name="Tag" size={11}/> Recategorize
                          </button>
                          <button className="btn btn--ghost btn--sm"
                                  title="Apply to every refunded pair from this merchant"
                                  onClick={() => setEditing(`merchant-${p.credit_id}`)}>
                            all
                          </button>
                        </div>
                      )}
                      {editing === `merchant-${p.credit_id}` && (
                        <CategoryPicker value={p.category_path || ''} options={catPaths}
                                        onPick={(cat) => recategorizeMerchant(p, cat)}
                                        onCancel={() => setEditing(null)}/>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}

function ActivityPage() {
  const { dataVersion } = useContext(AppContext)
  const [groups, setGroups] = useState([])
  const [summary, setSummary] = useState({})
  const [loading, setLoading] = useState(true)
  const [statusFilter, setStatusFilter] = useState('open')
  const [kindFilter, setKindFilter] = useState('')
  const [rebuilding, setRebuilding] = useState(false)
  const [msg, setMsg] = useState(null)
  const [expanded, setExpanded] = useState(null)
  const [detail, setDetail] = useState(null)
  const [noteDraft, setNoteDraft] = useState({})

  const load = useCallback(() => {
    setLoading(true)
    const params = new URLSearchParams()
    if (statusFilter) params.set('status', statusFilter)
    if (kindFilter) params.set('kind', kindFilter)
    apiFetch('/anomalies/groups?' + params.toString())
      .then(d => { setGroups(d.groups || []); setSummary(d.summary || {}); setLoading(false) })
      .catch(() => setLoading(false))
  }, [statusFilter, kindFilter])

  useEffect(() => { load() }, [load, dataVersion])

  async function handleRebuild() {
    setRebuilding(true); setMsg(null)
    try {
      const r = await apiPost('/anomalies/groups/rebuild', {})
      setMsg({ type: 'ok', text: `${r.raw_anomalies} raw signals grouped into ${r.groups} issues (${r.statuses_preserved} decisions kept)` })
      load()
    } catch (e) { setMsg({ type: 'err', text: String(e.message || e) }) }
    setRebuilding(false)
  }

  async function setStatus(group, status) {
    try {
      await apiFetch(`/anomalies/groups/${group.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status, resolution_note: noteDraft[group.id] || null }),
      })
      load()
    } catch (e) { setMsg({ type: 'err', text: String(e.message || e) }) }
  }

  async function toggleExpand(group) {
    if (expanded === group.id) { setExpanded(null); setDetail(null); return }
    setExpanded(group.id); setDetail(null)
    try { setDetail(await apiFetch(`/anomalies/groups/${group.id}`)) } catch { setDetail(null) }
  }

  const kinds = ['', 'category-anomaly', 'recurring-subscription', 'spending-spike',
                 'duplicate-candidate', 'new-merchant']
  const statuses = ['open', 'task', 'resolved', 'dismissed', '']
  const sevClass = s => s === 'high' ? 'neg' : s === 'medium' ? 'warn' : ''
  const outstanding = g => (g.total_cents || 0) - (g.refunded_cents || 0)

  return (
    <div className="page">
      <div className="page-intro">
        <div className="subtitle">
          Issues worth acting on — one per merchant or recurring problem, refund-aware.
          {summary.open ? ` ${summary.open.count} open · ${fmtCurrency(summary.open.outstanding_cents)} outstanding` : ' Nothing open.'}
        </div>
        <button className="btn btn--ghost" onClick={handleRebuild} disabled={rebuilding}>
          <Icon name="Repeat" size={14}/> {rebuilding ? 'Re-scanning…' : 'Re-scan'}
        </button>
      </div>

      {msg && (
        <div className="card" style={{padding:12, marginBottom:12,
             color: msg.type === 'err' ? 'var(--destructive)' : 'var(--muted-foreground)'}}>
          {msg.text}
        </div>
      )}

      <div className="filter-row">
        <span className="label">Status</span>
        {statuses.map(s => (
          <span key={s || 'all'} className={`chip ${statusFilter === s ? 'is-active' : ''}`}
                onClick={() => setStatusFilter(s)}>
            {s || 'All'}{summary[s] ? ` (${summary[s].count})` : ''}
          </span>
        ))}
      </div>
      <div className="filter-row" style={{marginTop:4}}>
        <span className="label">Kind</span>
        {kinds.map(k => (
          <span key={k || 'all'} className={`chip ${kindFilter === k ? 'is-active' : ''}`}
                onClick={() => setKindFilter(k)}>{k ? k.replace(/-/g, ' ') : 'All'}</span>
        ))}
      </div>

      {loading ? (
        <div className="card" style={{padding:20, marginTop:12}}>Scanning…</div>
      ) : groups.length === 0 ? (
        <div className="card" style={{padding:20, marginTop:12}}>
          Nothing here. {statusFilter === 'open' ? 'No open issues — everything has been resolved or dismissed.' : 'Try another filter.'}
        </div>
      ) : (
        <div style={{marginTop:12, display:'flex', flexDirection:'column', gap:10}}>
          {groups.map(g => (
            <div key={g.id} className="card" style={{padding:14}}>
              <div style={{display:'flex', gap:12, alignItems:'flex-start', flexWrap:'wrap'}}>
                <div style={{flex:1, minWidth:260}}>
                  <div style={{display:'flex', gap:8, alignItems:'center', flexWrap:'wrap'}}>
                    <span className={`chip ${sevClass(g.severity)}`}>{g.severity}</span>
                    <span className="chip">{(g.kind || '').replace(/-/g, ' ')}</span>
                    {g.status !== 'open' && <span className="chip is-active">{g.status}</span>}
                    {g.refund_status !== 'none' && (
                      <span className="chip">{g.refund_status === 'full' ? 'fully refunded' : 'partly refunded'}</span>
                    )}
                  </div>
                  <div style={{fontWeight:600, marginTop:6}}>{g.title}</div>
                  <div style={{color:'var(--muted-foreground)', fontSize:13, marginTop:2}}>{g.detail}</div>
                  <div style={{marginTop:8, fontSize:13}}>
                    <b>→ {g.suggested_action}</b>
                  </div>
                  {g.resolution_note && (
                    <div style={{marginTop:6, fontSize:12, color:'var(--muted-foreground)'}}>
                      Note: {g.resolution_note}
                    </div>
                  )}
                </div>
                <div style={{textAlign:'right', minWidth:150}}>
                  <div style={{fontSize:18, fontWeight:700}}>{fmtCurrency(outstanding(g))}</div>
                  <div style={{fontSize:11, color:'var(--muted-foreground)'}}>OUTSTANDING</div>
                  {g.refunded_cents > 0 && (
                    <div style={{fontSize:12, color:'var(--positive, #3fb950)', marginTop:4}}>
                      {fmtCurrency(g.refunded_cents)} refunded
                    </div>
                  )}
                  <div style={{fontSize:12, color:'var(--muted-foreground)', marginTop:4}}>
                    {g.transaction_count} txn{g.transaction_count === 1 ? '' : 's'}
                  </div>
                  <div style={{fontSize:11, color:'var(--muted-foreground)'}}>
                    {g.first_date}{g.last_date && g.last_date !== g.first_date ? ` → ${g.last_date}` : ''}
                  </div>
                </div>
              </div>

              <div style={{display:'flex', gap:8, marginTop:12, flexWrap:'wrap', alignItems:'center'}}>
                <input className="form-input" style={{maxWidth:280, height:30, fontSize:12}}
                       placeholder="Add a note (optional)"
                       value={noteDraft[g.id] || ''}
                       onChange={e => setNoteDraft(d => ({...d, [g.id]: e.target.value}))}/>
                {g.status !== 'task' && (
                  <button className="btn btn--ghost btn--sm" onClick={() => setStatus(g, 'task')}>
                    <Icon name="Target" size={12}/> Make a task
                  </button>
                )}
                {g.status !== 'resolved' && (
                  <button className="btn btn--primary btn--sm" onClick={() => setStatus(g, 'resolved')}>
                    Resolved
                  </button>
                )}
                {g.status !== 'dismissed' && (
                  <button className="btn btn--outline btn--sm" onClick={() => setStatus(g, 'dismissed')}>
                    Not an issue
                  </button>
                )}
                {g.status !== 'open' && (
                  <button className="btn btn--outline btn--sm" onClick={() => setStatus(g, 'open')}>
                    Reopen
                  </button>
                )}
                <button className="btn btn--ghost btn--sm" onClick={() => toggleExpand(g)}>
                  {expanded === g.id ? 'Hide' : 'Show'} transactions
                </button>
              </div>

              {expanded === g.id && (
                <div style={{marginTop:10}}>
                  {!detail ? <div style={{fontSize:12, color:'var(--muted-foreground)'}}>Loading…</div> : (
                    <table style={{width:'100%', fontSize:12}}>
                      <thead><tr>
                        <th style={{textAlign:'left'}}>Date</th>
                        <th style={{textAlign:'left'}}>Merchant</th>
                        <th style={{textAlign:'right'}}>Amount</th>
                        <th style={{textAlign:'left'}}>Role</th>
                      </tr></thead>
                      <tbody>
                        {(detail.transactions || []).map(t => (
                          <tr key={`${t.id}-${t.role}`}>
                            <td>{t.posted_date}</td>
                            <td>{(t.merchant || t.description || '').slice(0, 44)}</td>
                            <td style={{textAlign:'right'}} className={t.amount_cents > 0 ? 'pos' : ''}>
                              {fmtCurrency(t.amount_cents)}
                            </td>
                            <td>{t.role}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ───── Mortgage Page ─────
// Rewritten to read GET /api/mortgage, which reports the loan from the ledger.
// What was here before rendered the `amortization` table — a forward projection
// the lender issued in 2026 — as if it were history, so a loan that was taken out
// in February 2025 and paid off in July 2026 appeared to start in mid-2026, run
// to 2031 and still owe $153,538. Every figure now carries a basis (measured /
// derived / stated / inferred / unknown) and nothing on the page is invented:
// where a fact is not known the tab says so and offers to record it.

const BASIS_CHIP = {
  measured: { cls: 'is-good', label: 'measured' },
  derived: { cls: 'is-plain', label: 'derived' },
  stated: { cls: 'is-plain', label: 'stated by you' },
  inferred: { cls: 'is-warn', label: 'inferred' },
  // The transaction is real; what it was FOR is the owner's recollection.
  believed: { cls: 'is-warn', label: 'believed' },
  unknown: { cls: 'is-warn', label: 'not known' },
}

function BasisChip({ basis }) {
  const b = BASIS_CHIP[basis] || BASIS_CHIP.unknown
  return <span className={`chip is-plain ${b.cls}`} style={{height:20, fontSize:10.5, padding:'0 8px'}}>{b.label}</span>
}

// A KPI whose subtitle IS its provenance. Unknown figures render as an em dash,
// never as $0 — a paid-off escrow refund we have not seen is not zero dollars.
function FigureCard({ label, figure, color, children }) {
  const known = figure && figure.cents != null
  return (
    <div className="card kpi">
      <div className="kpi-label">{label}</div>
      <div className="kpi-value" style={{color: known ? color : 'var(--muted-foreground)'}}>
        {known ? fmtCurrency(figure.cents, {full: true, decimals: 2}) : '—'}
      </div>
      <div className="kpi-delta" style={{flexWrap:'wrap', gap:6}}>
        <BasisChip basis={figure?.basis}/>
        {children}
      </div>
      {figure?.note && (
        <div style={{fontSize:11.5, color:'var(--muted-foreground)', marginTop:6, lineHeight:1.45}}>
          {figure.note}
        </div>
      )}
    </div>
  )
}

function MortgagePage() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState(null)
  const [showProjection, setShowProjection] = useState(false)
  const [editing, setEditing] = useState(false)
  const [saving, setSaving] = useState(false)
  const [form, setForm] = useState({})

  const load = useCallback(() => {
    setLoading(true)
    return apiFetch('/mortgage')
      .then(d => { setData(d); setErr(null) })
      .catch(e => setErr(e.message))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])

  function openEditor() {
    const f = data?.facts || {}
    setForm({
      original_principal_cents: f.original_principal_cents ?? '',
      annual_rate_bps: f.annual_rate_bps ?? '',
      term_months: f.term_months ?? '',
      first_payment_date: f.first_payment_date ?? '',
      escrow_refund_cents: f.escrow_refund_cents ?? '',
      escrow_refund_date: f.escrow_refund_date ?? '',
    })
    setEditing(true)
  }

  async function saveFacts(e) {
    e.preventDefault()
    setSaving(true)
    const body = {}
    for (const [k, v] of Object.entries(form)) {
      body[k] = v === '' ? null : (k.endsWith('_cents') || k.endsWith('_bps') || k.endsWith('_months') ? Number(v) : v)
    }
    try {
      await apiFetch('/mortgage/facts', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      setEditing(false)
      await load()
    } catch (e2) {
      setErr(e2.message)
    } finally {
      setSaving(false)
    }
  }

  if (loading && !data) {
    return <div className="page"><div className="subtitle">Loading the loan…</div></div>
  }
  if (err && !data) {
    return <div className="page"><div className="subtitle">Could not load the mortgage: {err}</div></div>
  }

  const f = data.figures || {}
  const terms = data.terms || {}
  const timeline = data.timeline || []
  const checks = data.checks || []
  const closed = data.status === 'closed'
  const escrowKnown = f.escrow_refunded?.cents != null

  return (
    <div className="page">
      <div className="page-intro">
        <div className="subtitle">
          {closed
            ? <>Paid off on <strong>{data.payoff_date}</strong>{terms.scheduled_payoff_date
                ? <> — {data.months_saved} months ahead of the original {terms.scheduled_payoff_date} payoff</>
                : null}. Every figure below is labelled with where it came from.</>
            : <>Loan position from the transactions on record. Every figure is labelled with where it came from.</>}
        </div>
        <div className="page-intro-actions">
          {closed && <span className="chip is-plain is-good"><Icon name="Check" size={12}/> Paid off</span>}
          <button className="btn btn--outline btn--sm" onClick={openEditor}>
            <Icon name="Settings" size={13}/> Loan facts
          </button>
        </div>
      </div>

      {err && <div className="card" style={{padding:14, color:'var(--destructive)'}}>{err}</div>}

      <div className="kpi-grid">
        <FigureCard label="Amount borrowed" figure={f.original_loan} color="#a78bfa"/>
        <FigureCard label="Total paid to lender" figure={f.total_paid_to_lender} color="var(--foreground)"/>
        <FigureCard label="Principal repaid" figure={f.principal_paid} color="#4ade80"/>
        <FigureCard label="Interest paid" figure={f.interest_paid} color="#f43f5e"/>
      </div>

      <div className="kpi-grid">
        <FigureCard label="Escrow paid in" figure={f.escrow_paid} color="#38bdf8"/>
        <FigureCard label="Escrow refunded" figure={f.escrow_refunded} color="#38bdf8">
          {!escrowKnown && (
            <button className="btn btn--outline btn--xs" onClick={openEditor}>Record it</button>
          )}
        </FigureCard>
        <FigureCard label="Balance now" figure={f.final_balance} color={closed ? '#4ade80' : '#facc15'}/>
        <FigureCard label="Interest saved" figure={f.interest_saved_vs_plan} color="#4ade80"/>
      </div>

      {/* Why the loan closed early — the single most interesting fact about it. */}
      <div className="card">
        <div className="section-head">
          <div>
            <div className="h">How it was paid off early</div>
            <div className="sub">
              Scheduled payments against extra principal. Curtailments go 100% to principal,
              which is what pulled the payoff forward.
            </div>
          </div>
        </div>
        <div className="card-body" style={{display:'grid', gap:12, gridTemplateColumns:'repeat(auto-fit, minmax(220px, 1fr))'}}>
          {[
            ['Scheduled monthly payments', f.scheduled_payments_paid],
            ['Extra principal (curtailments)', f.extra_principal_paid],
            ['Interest the original plan would have charged', f.plan_interest_total],
          ].map(([label, fig]) => (
            <div key={label}>
              <div className="kpi-label" style={{fontSize:11}}>{label}</div>
              <div className="mono" style={{fontSize:20, fontWeight:600, marginTop:4}}>
                {fig?.cents != null ? fmtCurrency(fig.cents, {full: true, decimals: 2}) : '—'}
              </div>
              <div style={{marginTop:4}}><BasisChip basis={fig?.basis}/></div>
              {fig?.note && <div style={{fontSize:11.5, color:'var(--muted-foreground)', marginTop:4}}>{fig.note}</div>}
            </div>
          ))}
        </div>
      </div>

      <div className="card">
        <div className="section-head">
          <div>
            <div className="h">Balance: what happened vs the original plan</div>
            <div className="sub">
              Solid line is the real balance after every payment on record. Dashed line is the
              loan as written ({terms.term_months || '—'} payments of{' '}
              {terms.scheduled_pi_cents != null ? fmtCurrency(terms.scheduled_pi_cents, {full:true, decimals:2}) : '—'} P&amp;I
              at {terms.annual_rate_pct != null ? terms.annual_rate_pct + '%' : '—'}) — the plan, not a forecast of anything still to come.
            </div>
          </div>
        </div>
        <div className="card-body">
          <MortgageBalanceChart timeline={timeline} plan={data.plan_schedule || []}
                                startCents={f.original_loan?.cents}
                                startDate={data.facts?.first_payment_date || data.first_payment_date}/>
        </div>
      </div>

      <div className="card">
        <div className="section-head">
          <div>
            <div className="h">Every payment to the servicer</div>
            <div className="sub">
              {timeline.length} payments, {data.scope} scope. The principal / interest / escrow
              columns are derived by running the amortization over these exact dates — they are
              not stored on the transactions.
            </div>
          </div>
        </div>
        <div style={{overflow:'auto', maxHeight:460}}>
          <table className="tbl">
            <thead><tr>
              <th>Date</th><th>Type</th><th className="text-right">Paid</th>
              <th className="text-right">Principal</th><th className="text-right">Interest</th>
              <th className="text-right">Escrow</th><th className="text-right">Balance after</th>
            </tr></thead>
            <tbody>
              {timeline.map(t => (
                <tr key={t.id}>
                  <td className="mono" style={{fontSize:12}}>{t.posted_date}</td>
                  <td>
                    <span className={`chip is-plain ${t.kind === 'extra' ? 'is-good' : ''}`}
                          style={{height:20, fontSize:10.5, padding:'0 8px'}}>
                      {t.kind === 'extra' ? 'extra principal' : 'monthly'}
                    </span>
                  </td>
                  <td className="text-right mono">{fmtCurrency(t.amount_cents, {full:true, decimals:2})}</td>
                  <td className="text-right mono">{t.principal_cents == null ? '—' : fmtCurrency(t.principal_cents, {full:true, decimals:2})}</td>
                  <td className="text-right mono">{t.interest_cents == null ? '—' : fmtCurrency(t.interest_cents, {full:true, decimals:2})}</td>
                  <td className="text-right mono">{t.escrow_cents == null ? '—' : fmtCurrency(t.escrow_cents, {full:true, decimals:2})}</td>
                  <td className="text-right mono">{t.balance_after_cents == null ? '—' : fmtCurrency(t.balance_after_cents, {full:true, decimals:2})}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {checks.length > 0 && (
        <div className="card">
          <div className="section-head">
            <div>
              <div className="h">Cross-checks</div>
              <div className="sub">
                Numbers that had no reason to agree unless this reconstruction of the loan is right.
              </div>
            </div>
          </div>
          <table className="tbl">
            <tbody>
              {checks.map(c => (
                <tr key={c.name}>
                  <td style={{width:26}}>
                    <span style={{color: c.agrees ? '#4ade80' : 'var(--destructive)'}}>
                      <Icon name={c.agrees ? 'Check' : 'AlertTriangle'} size={14}/>
                    </span>
                  </td>
                  <td style={{fontWeight:500}}>{c.name}</td>
                  <td style={{fontSize:12, color:'var(--muted-foreground)'}}>
                    {c.left_label}: <span className="mono">{c.unit === 'percent' ? c.left + '%' : fmtCurrency(c.left, {full:true, decimals:2})}</span>
                    {' · '}
                    {c.right_label}: <span className="mono">{c.unit === 'percent' ? c.right + '%' : fmtCurrency(c.right, {full:true, decimals:2})}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {(data.related || []).length > 0 && (
        <div className="card">
          <div className="section-head">
            <div>
              <div className="h">At the closing table</div>
              <div className="sub">
                Related to buying the house, but NOT part of the loan — none of it is in the
                totals above.
              </div>
            </div>
          </div>
          <table className="tbl">
            <tbody>
              {data.related.map(r => (
                <tr key={r.id}>
                  <td className="mono" style={{fontSize:12, width:110, whiteSpace:'nowrap'}}>{r.posted_date}</td>
                  <td className="text-right mono" style={{width:140}}>{fmtCurrency(r.amount_cents, {full:true, decimals:2})}</td>
                  <td>
                    <div>{r.label}</div>
                    <div style={{fontSize:11.5, color:'var(--muted-foreground)', marginTop:2}}>
                      {(r.description || '').slice(0, 70)}
                      {r.implied_closing_costs_cents != null && f.down_payment?.cents != null && (
                        <> — {fmtCurrency(f.down_payment.cents, {full:true, decimals:2})} down plus{' '}
                        {fmtCurrency(r.implied_closing_costs_cents, {full:true, decimals:2})} that would be closing costs</>
                      )}
                    </div>
                  </td>
                  <td style={{width:110}}><BasisChip basis={r.confidence || 'believed'}/></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="card">
        <div className="section-head">
          <div>
            <div className="h">Loan terms</div>
            <div className="sub">Terms come from the closing documents, so they are recorded, not guessed.</div>
          </div>
          <button className="btn btn--outline btn--sm" onClick={openEditor}>Edit</button>
        </div>
        <table className="tbl">
          <tbody>
            {[
              ['Amount borrowed', f.original_loan?.cents != null ? fmtCurrency(f.original_loan.cents, {full:true, decimals:2}) : '—', f.original_loan?.basis],
              ['Rate', terms.annual_rate_pct != null ? terms.annual_rate_pct + '% a year' : '—', terms.annual_rate_basis],
              ['Term', terms.term_months ? terms.term_months + ' months' : '—', terms.term_months_basis],
              ['Scheduled P&I', terms.scheduled_pi_cents != null ? fmtCurrency(terms.scheduled_pi_cents, {full:true, decimals:2}) + ' a month' : '—', terms.scheduled_pi_basis],
              ['First payment', data.facts?.first_payment_date || data.first_payment_date || '—', data.facts?.first_payment_date ? 'stated' : data.first_payment_basis],
              ['Original payoff date', terms.scheduled_payoff_date || '—', terms.scheduled_payoff_date_basis],
              ['Actual payoff date', data.payoff_date || '—', data.payoff_date_basis],
              ['Purchase price', f.purchase_price?.cents != null ? fmtCurrency(f.purchase_price.cents, {full:true, decimals:2}) : '—', f.purchase_price?.basis],
              ['Down payment', f.down_payment?.cents != null ? fmtCurrency(f.down_payment.cents, {full:true, decimals:2}) : '—', f.down_payment?.basis],
            ].map(([label, value, basis]) => (
              <tr key={label}>
                <td style={{width:200, color:'var(--muted-foreground)'}}>{label}</td>
                <td className="mono">{value}</td>
                <td style={{width:120}}><BasisChip basis={basis}/></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {(data.notes || []).length > 0 && (
        <div className="card" style={{padding:14}}>
          <div className="h" style={{marginBottom:6}}>How these numbers are derived</div>
          <ul style={{margin:0, paddingLeft:18, fontSize:12.5, color:'var(--muted-foreground)', lineHeight:1.6}}>
            {data.notes.map((n, i) => <li key={i}>{n}</li>)}
          </ul>
        </div>
      )}

      {(data.warnings || []).length > 0 && (
        <div className="card" style={{padding:14}}>
          <div className="h" style={{marginBottom:6}}><Icon name="AlertTriangle" size={13}/> Worth knowing</div>
          <ul style={{margin:0, paddingLeft:18, fontSize:12.5, color:'#facc15', lineHeight:1.6}}>
            {data.warnings.map((n, i) => <li key={i}>{n}</li>)}
          </ul>
        </div>
      )}

      {/* The lender's projection, kept only as the superseded document it is. */}
      <div className="card">
        <div className="section-head">
          <div>
            <div className="h">Lender's forward projection ({(data.lender_projection || []).length} months)</div>
            <div className="sub">
              Superseded. This is the schedule the servicer issued
              {data.lender_projection?.[0] ? ` from ${data.lender_projection[0].month.slice(0, 7)}` : ''} —
              a projection of payments that were never made, because the loan was paid off
              {closed ? ` on ${data.payoff_date}` : ''}. Shown for reference only; nothing above uses it as history.
            </div>
          </div>
          <button className="btn btn--ghost btn--sm" onClick={() => setShowProjection(s => !s)}>
            {showProjection ? 'Hide' : 'Show'}
          </button>
        </div>
        {showProjection && (
          <div style={{overflow:'auto', maxHeight:360}}>
            <table className="tbl">
              <thead><tr>
                <th>Month</th><th className="text-right">Principal</th>
                <th className="text-right">Interest</th><th className="text-right">Balance</th>
              </tr></thead>
              <tbody>
                {(data.lender_projection || []).map(r => (
                  <tr key={r.id || r.month}>
                    <td className="mono" style={{fontSize:12}}>{r.month}</td>
                    <td className="text-right mono">{fmtCurrency(r.principal_cents, {full:true, decimals:2})}</td>
                    <td className="text-right mono">{fmtCurrency(r.interest_cents, {full:true, decimals:2})}</td>
                    <td className="text-right mono">{fmtCurrency(r.ending_balance_cents, {full:true, decimals:2})}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {editing && (
        <div className="modal-overlay" onClick={() => setEditing(false)}>
          <div className="modal" onClick={e => e.stopPropagation()}>
            <div className="modal-header">
              <div className="modal-title">Loan facts</div>
              <button className="btn btn--ghost btn--sm" onClick={() => setEditing(false)}><Icon name="X" size={14}/></button>
            </div>
            <form onSubmit={saveFacts}>
              <div className="modal-body">
                <p style={{fontSize:12, color:'var(--muted-foreground)', marginTop:0}}>
                  These come off the closing documents and the payoff letter — nothing in a bank
                  export carries them. Anything you set here is reported as “stated by you” and
                  overrides what the app worked out. Clear a field to hand it back to the derivation.
                </p>
                <div className="form-row">
                  <div className="form-group">
                    <label className="form-label">Amount borrowed (cents)</label>
                    <input className="form-input" type="number" value={form.original_principal_cents}
                           onChange={e => setForm(s => ({...s, original_principal_cents: e.target.value}))}
                           placeholder="e.g. 28000000 for $280,000"/>
                  </div>
                  <div className="form-group">
                    <label className="form-label">Rate (basis points)</label>
                    <input className="form-input" type="number" value={form.annual_rate_bps}
                           onChange={e => setForm(s => ({...s, annual_rate_bps: e.target.value}))}
                           placeholder="e.g. 649 for 6.49%"/>
                  </div>
                </div>
                <div className="form-row">
                  <div className="form-group">
                    <label className="form-label">Term (months)</label>
                    <input className="form-input" type="number" value={form.term_months}
                           onChange={e => setForm(s => ({...s, term_months: e.target.value}))} placeholder="120"/>
                  </div>
                  <div className="form-group">
                    <label className="form-label">First payment date</label>
                    <input className="form-input" type="date" value={form.first_payment_date}
                           onChange={e => setForm(s => ({...s, first_payment_date: e.target.value}))}/>
                  </div>
                </div>
                <div className="form-row">
                  <div className="form-group">
                    <label className="form-label">Escrow refunded (cents)</label>
                    <input className="form-input" type="number" value={form.escrow_refund_cents}
                           onChange={e => setForm(s => ({...s, escrow_refund_cents: e.target.value}))}
                           placeholder="e.g. 160000 for $1,600"/>
                  </div>
                  <div className="form-group">
                    <label className="form-label">Escrow refund date</label>
                    <input className="form-input" type="date" value={form.escrow_refund_date}
                           onChange={e => setForm(s => ({...s, escrow_refund_date: e.target.value}))}/>
                  </div>
                </div>
                <p style={{fontSize:12, color:'var(--muted-foreground)', marginBottom:0}}>
                  The escrow refund is not in the ledger. Recording it here keeps it labelled as
                  your figure; if the deposit later syncs from the bank, the measured credit wins.
                </p>
              </div>
              <div className="modal-foot">
                <button type="button" className="btn btn--outline" onClick={() => setEditing(false)}>Cancel</button>
                <button type="submit" className="btn btn--primary" disabled={saving}>
                  {saving ? 'Saving…' : 'Save facts'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  )
}

// Real balance after every payment, against the loan as originally written. Two
// series on one time axis (x is the date, not the row index — the curtailments
// are days apart, and evenly spacing them would misrepresent how fast the
// balance actually fell).
function MortgageBalanceChart({ timeline, plan, startCents, startDate }) {
  const ref = useRef(null)
  const [w, setW] = useState(800)
  const [hov, setHov] = useState(null)
  useEffect(() => {
    if (!ref.current) return
    const ro = new ResizeObserver(es => { for (const e of es) setW(e.contentRect.width) })
    ro.observe(ref.current)
    return () => ro.disconnect()
  }, [])

  const height = 260
  const pad = { l: 60, r: 16, t: 12, b: 30 }
  const innerW = Math.max(w - pad.l - pad.r, 10)
  const innerH = height - pad.t - pad.b

  // Both series open at the amount borrowed, so the drop from it is visible
  // rather than implied by an axis that starts below it.
  const origin = (startCents != null && startDate)
    ? [{ t: new Date(startDate + 'T00:00:00').getTime(), v: startCents, label: startDate, kind: 'origination' }]
    : []
  const actual = origin.concat(timeline
    .filter(t => t.balance_after_cents != null)
    .map(t => ({ t: new Date(t.posted_date + 'T00:00:00').getTime(), v: t.balance_after_cents, label: t.posted_date, kind: t.kind })))
  const planPts = origin.concat((plan || [])
    .filter(p => p.date)
    .map(p => ({ t: new Date(p.date + 'T00:00:00').getTime(), v: p.ending_balance_cents, label: p.date })))

  if (!actual.length) {
    return <div className="subtitle">No payments to the servicer in this scope, so there is no balance curve to draw.</div>
  }

  // The plan runs years past the payoff. Showing all of it would squash the part
  // that actually happened, so the x axis stops a year after the last real
  // payment and the plan line is clipped to it.
  const tStart = Math.min(actual[0].t, planPts[0]?.t ?? actual[0].t)
  const tEnd = Math.max(...actual.map(a => a.t)) + 365 * 24 * 3600 * 1000
  const planClipped = planPts.filter(p => p.t <= tEnd)
  const maxV = Math.max(...actual.map(a => a.v), ...planClipped.map(p => p.v), 1)
  const xMap = t => pad.l + ((t - tStart) / Math.max(tEnd - tStart, 1)) * innerW
  const yMap = v => pad.t + (1 - v / maxV) * innerH
  const toPath = pts => pts.map((p, i) => (i === 0 ? 'M' : 'L') + xMap(p.t).toFixed(1) + ',' + yMap(p.v).toFixed(1)).join(' ')

  const ticks = 4
  const yTicks = Array.from({length: ticks + 1}, (_, i) => (maxV * i) / ticks)
  const xTicks = [0, 0.25, 0.5, 0.75, 1].map(fr => tStart + fr * (tEnd - tStart))

  return (
    <div ref={ref} style={{position:'relative'}}>
      <svg className="area-chart" viewBox={`0 0 ${w} ${height}`} height={height}
           onMouseMove={e => {
             const rect = ref.current.querySelector('svg').getBoundingClientRect()
             const xPx = (e.clientX - rect.left) * (w / rect.width)
             let best = null
             for (const a of actual) {
               const d = Math.abs(xMap(a.t) - xPx)
               if (!best || d < best.d) best = { d, a }
             }
             if (best) setHov({ x: xMap(best.a.t), y: yMap(best.a.v), p: best.a })
           }}
           onMouseLeave={() => setHov(null)}>
        {yTicks.map((t, i) => (
          <g key={i}>
            <line className="chart-grid" x1={pad.l} x2={w - pad.r} y1={yMap(t)} y2={yMap(t)}/>
            <text className="chart-label" x={pad.l - 8} y={yMap(t)} textAnchor="end" dominantBaseline="central">
              {fmtCurrency(t)}
            </text>
          </g>
        ))}
        {xTicks.map((t, i) => (
          <text key={i} className="chart-label" x={xMap(t)} y={pad.t + innerH + 18} textAnchor="middle">
            {fmtMonth(new Date(t))}
          </text>
        ))}
        {planClipped.length > 1 && (
          <path d={toPath(planClipped)} fill="none" stroke="#a78bfa" strokeWidth={1.5}
                strokeDasharray="4 4" strokeLinejoin="round"/>
        )}
        <path d={toPath(actual)} fill="none" stroke="#4ade80" strokeWidth={2} strokeLinejoin="round"/>
        {actual.filter(a => a.kind === 'extra').map(a => (
          <circle key={a.t} cx={xMap(a.t)} cy={yMap(a.v)} r={3} fill="#4ade80" stroke="var(--card)" strokeWidth={1.5}/>
        ))}
        {hov && (
          <>
            <line x1={hov.x} x2={hov.x} y1={pad.t} y2={pad.t + innerH} stroke="var(--foreground)" strokeOpacity="0.3" strokeDasharray="2 3"/>
            <circle cx={hov.x} cy={hov.y} r={4} fill="#4ade80" stroke="var(--card)" strokeWidth={2}/>
          </>
        )}
      </svg>
      {hov && (
        <div className="tooltip" style={{left: hov.x + 12, top: hov.y + 12, position:'absolute'}}>
          <div className="t-label">{hov.p.label} · {hov.p.kind === 'extra' ? 'extra principal' : 'monthly payment'}</div>
          <div className="t-value">{fmtCurrency(hov.p.v, {full:true, decimals:2})} left</div>
        </div>
      )}
      <div style={{display:'flex', gap:14, fontSize:12, color:'var(--muted-foreground)', padding:'6px 0 0 60px'}}>
        <span><span className="swatch" style={{background:'#4ade80'}}/> Actual balance (dots are extra principal)</span>
        <span><span className="swatch" style={{background:'#a78bfa'}}/> The loan as originally written</span>
      </div>
    </div>
  )
}

// ───── Accounts & Sync ─────
// Was public/accounts.html, a standalone page outside the shell. The only thing
// that kept it out here was Plaid Link's CDN script, which we now inject on
// demand instead of carrying a remote <script> in index.html.
const PLAID_LINK_SRC = 'https://cdn.plaid.com/link/v2/stable/link-initialize.js'
let plaidLinkPromise = null
function loadPlaidLink() {
  if (window.Plaid) return Promise.resolve(window.Plaid)
  if (!plaidLinkPromise) {
    plaidLinkPromise = new Promise((resolve, reject) => {
      const el = document.createElement('script')
      el.src = PLAID_LINK_SRC
      el.async = true
      el.onload = () => window.Plaid
        ? resolve(window.Plaid)
        : reject(new Error('Plaid Link loaded but window.Plaid is missing'))
      el.onerror = () => { plaidLinkPromise = null; reject(new Error('Plaid Link script did not load (offline?)')) }
      document.head.appendChild(el)
    })
  }
  return plaidLinkPromise
}

function syncFreshness(iso) {
  if (!iso) return <span className="chip is-plain is-warn">never synced</span>
  const mins = Math.round((Date.now() - new Date(iso + 'Z').getTime()) / 60000)
  if (Number.isNaN(mins)) return <span className="chip is-plain">{iso}</span>
  if (mins < 90) return <span className="chip is-plain is-good">{mins} min ago</span>
  const hrs = Math.round(mins / 60)
  return <span className={`chip is-plain ${hrs < 30 ? '' : 'is-warn'}`}>{hrs} h ago</span>
}

function itemStatusChip(status) {
  const cls = status === 'ACTIVE' ? 'is-good' : status === 'LOGIN_REQUIRED' ? 'is-warn' : 'is-bad'
  return <span className={`chip is-plain ${cls}`}>{status}</span>
}

function AccountsPage() {
  const [plaid, setPlaid] = useState(null)
  const [plaidErr, setPlaidErr] = useState(null)
  const [amazon, setAmazon] = useState(null)
  const [amazonErr, setAmazonErr] = useState(null)
  const [busy, setBusy] = useState(null)   // which action is in flight
  const [log, setLog] = useState(['Ready.'])
  // Draft canonical-account ids, so typing in the input doesn't fight the poll.
  const [drafts, setDrafts] = useState({})

  const addLog = useCallback((msg, obj) => {
    const at = new Date().toLocaleTimeString()
    const body = obj === undefined ? '' : ' ' + JSON.stringify(obj)
    setLog(l => [`[${at}] ${msg}${body}`, ...l].slice(0, 200))
  }, [])

  const loadPlaid = useCallback(() => (
    apiFetch('/plaid/status')
      .then(d => { setPlaid(d); setPlaidErr(null); setDrafts({}) })
      .catch(e => setPlaidErr(e.message))
  ), [])

  const loadAmazon = useCallback(() => (
    apiFetch('/amazon/status')
      .then(d => { setAmazon(d); setAmazonErr(null) })
      .catch(e => setAmazonErr(e.message))
  ), [])

  useEffect(() => { loadPlaid(); loadAmazon() }, [loadPlaid, loadAmazon])

  async function handleConnect() {
    setBusy('connect')
    try {
      addLog('Requesting Plaid link token…')
      const { link_token } = await apiPost('/plaid/link-token', {})
      const Plaid = await loadPlaidLink()
      Plaid.create({
        token: link_token,
        onSuccess: async (public_token, meta) => {
          addLog('Link succeeded, exchanging token…', { institution: meta?.institution?.name })
          try {
            addLog('Bank linked', await apiPost('/plaid/exchange', { public_token }))
            await loadPlaid()
            addLog('Now click "Sync now" to pull transactions (up to 730 days).')
          } catch (e) { addLog('Exchange failed: ' + e.message) }
        },
        onExit: (err) => addLog('Link closed' + (err ? ': ' + err.error_message : '')),
      }).open()
    } catch (e) { addLog('Could not start Plaid Link: ' + e.message) }
    setBusy(null)
  }

  async function handleSync() {
    setBusy('sync')
    try {
      addLog('Syncing… (first sync of 730 days can take a while)')
      addLog('Sync complete', await apiPost('/plaid/sync', {}))
      await loadPlaid()
    } catch (e) { addLog('Sync failed: ' + e.message) }
    setBusy(null)
  }

  async function saveMapping(acct, value) {
    try {
      await apiFetch(`/plaid/accounts/${acct.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ canonical_account_id: value.trim() }),
      })
      addLog('Account mapping saved', { id: acct.id, account_id: value.trim() })
    } catch (e) { addLog('Mapping failed: ' + e.message) }
  }

  async function handleUnlink(item) {
    if (!window.confirm('Unlink this bank? Transactions already imported are kept.')) return
    setBusy('unlink-' + item.id)
    try {
      addLog('Unlinking…', await apiFetch(`/plaid/items/${item.id}`, { method: 'DELETE' }))
      await loadPlaid()
    } catch (e) { addLog('Unlink failed: ' + e.message) }
    setBusy(null)
  }

  async function amazonAction(key, path, label) {
    setBusy(key)
    try {
      addLog(label + '…')
      addLog(label + ' done', await apiPost(path, {}))
      await loadAmazon()
    } catch (e) { addLog(label + ' failed: ' + e.message) }
    setBusy(null)
  }

  const items = plaid?.items || []
  const amzPct = amazon?.items ? Math.round((amazon.items_matched / amazon.items) * 100) : 0

  return (
    <div className="page">
      <div className="page-intro">
        <div className="subtitle">Live bank data via Plaid, plus Amazon order detail</div>
      </div>

      <div className="card">
        <div className="section-head">
          <div>
            <div className="h">Plaid</div>
            <div className="sub">Link a bank once, then sync to pull transactions</div>
          </div>
          <div className="page-intro-actions">
            <button className="btn btn--primary" onClick={handleConnect}
                    disabled={!plaid?.configured || busy === 'connect'}>
              <Icon name="Plus" size={14}/> Connect a bank
            </button>
            <button className="btn btn--outline" onClick={handleSync} disabled={busy === 'sync'}>
              <Icon name="Repeat" size={14}/> {busy === 'sync' ? 'Syncing…' : 'Sync now'}
            </button>
          </div>
        </div>
        <div className="card-body">
          {plaidErr ? <div className="msg-banner err">{plaidErr}</div> : (
            <div className="kpi-grid" style={{gridTemplateColumns:'repeat(4, 1fr)'}}>
              <KpiCard label="Configured" value={plaid?.configured ? 'yes' : 'no'} color="#38bdf8"/>
              <KpiCard label="Environment" value={plaid?.environment || '—'} color="#a78bfa"/>
              <KpiCard label="Token encryption" value={plaid?.encryption_configured ? 'on' : 'off'} color="#4ade80"/>
              <KpiCard label="Linked items" value={String(plaid?.item_count ?? 0)} color="#facc15"/>
            </div>
          )}

          {!plaidErr && items.length === 0 && (
            <div className="subtitle" style={{marginTop:16}}>
              {plaid?.configured
                ? 'No banks linked yet. Click Connect a bank to add your accounts.'
                : 'Set PLAID_CLIENT_ID and a matching secret in .env first.'}
            </div>
          )}

          {items.length > 0 && (
            <div style={{overflowX:'auto', marginTop:16}}>
              <table className="tbl">
                <thead>
                  <tr>
                    <th>Institution</th><th>Status</th><th>Last sync</th><th>Accounts</th><th/>
                  </tr>
                </thead>
                <tbody>
                  {items.map(it => (
                    <tr key={it.id} style={{cursor:'default'}}>
                      <td style={{fontWeight:500}}>{it.institution_name || it.institution_id || 'Bank'}</td>
                      <td>
                        {itemStatusChip(it.status)}
                        {it.error_code && <span className="chip is-plain is-bad" style={{marginLeft:6}}>{it.error_code}</span>}
                      </td>
                      <td>{syncFreshness(it.last_synced_at)}</td>
                      <td>
                        <div style={{display:'flex', flexDirection:'column', gap:8}}>
                          {(it.accounts || []).map(a => (
                            <div key={a.id} style={{display:'flex', alignItems:'center', gap:8, flexWrap:'wrap'}}>
                              <span className="chip is-plain mono">••{a.mask || '????'}</span>
                              <span>{a.name || ''}</span>
                              <input className="form-input" style={{width:150, padding:'5px 8px'}}
                                     title="Account id used by the app"
                                     value={drafts[a.id] ?? a.canonical_account_id ?? ''}
                                     onChange={e => setDrafts(d => ({...d, [a.id]: e.target.value}))}
                                     onBlur={e => saveMapping(a, e.target.value)}
                                     onKeyDown={e => { if (e.key === 'Enter') e.currentTarget.blur() }}/>
                              <span className="chip is-plain">{a.include_in_sync ? 'syncing' : 'excluded'}</span>
                              <span className="muted mono" style={{fontSize:12}}>
                                {a.current_balance_cents != null ? fmtCurrency(a.current_balance_cents, {full:true}) : ''}
                              </span>
                            </div>
                          ))}
                        </div>
                      </td>
                      <td className="text-right">
                        <button className="btn btn--outline btn--sm" onClick={() => handleUnlink(it)}
                                disabled={busy === 'unlink-' + it.id}>
                          <Icon name="Trash" size={13}/> Unlink
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      <div className="card">
        <div className="section-head">
          <div>
            <div className="h">Amazon</div>
            <div className="sub">Order detail matched to card charges, then split into items</div>
          </div>
          <div className="page-intro-actions">
            <button className="btn btn--outline" disabled={busy === 'amz-import'}
                    onClick={() => amazonAction('amz-import', '/amazon/import', 'Amazon import')}>
              <Icon name="Upload" size={14}/> Import export
            </button>
            <button className="btn btn--outline" disabled={busy === 'amz-match'}
                    onClick={() => amazonAction('amz-match', '/amazon/match', 'Amazon match')}>
              <Icon name="GitMerge" size={14}/> Match charges
            </button>
            <button className="btn btn--outline" disabled={busy === 'amz-split'}
                    onClick={() => amazonAction('amz-split', '/amazon/split', 'Amazon split')}>
              <Icon name="Filter" size={14}/> Split into items
            </button>
          </div>
        </div>
        <div className="card-body">
          {amazonErr ? <div className="msg-banner err">{amazonErr}</div> : (
            <div className="kpi-grid" style={{gridTemplateColumns:'repeat(3, 1fr)'}}>
              <KpiCard label="Exports on disk" value={(amazon?.exports_on_disk || []).join(', ') || '—'} color="#38bdf8"/>
              <KpiCard label="Items" value={String(amazon?.items ?? 0)} color="#a78bfa"/>
              <KpiCard label="Matched" value={`${amazon?.items_matched ?? 0} (${amzPct}%)`} color="#4ade80"/>
              <KpiCard label="Split rows" value={String(amazon?.items_split ?? 0)} color="#facc15"/>
              <KpiCard label="Refunds" value={`${amazon?.refunds_matched ?? 0}/${amazon?.refunds ?? 0}`} color="#94a3b8"/>
              <KpiCard label="Refunded back" value={fmtCurrency(amazon?.matched_refund_value_cents || 0)} color="#4ade80"/>
            </div>
          )}
          <div className="subtitle" style={{marginTop:16}}>
            Unpack <code>Your Orders.zip</code> into <code>samples/amazon/</code>{' '}
            (or <code>samples/amazon/&lt;person&gt;/</code> for a second account), then Import.
          </div>
        </div>
      </div>

      <div className="card">
        <div className="section-head">
          <div>
            <div className="h">Activity</div>
            <div className="sub">What this page has done, newest first</div>
          </div>
        </div>
        <div className="card-body">
          <div className="activity-log">{log.join('\n')}</div>
        </div>
      </div>
    </div>
  )
}

// ───── Import Banner ─────
function ImportBanner({ status }) {
  const pct = status?.progress_pct ?? 0
  const msg = status?.message || 'Importing data...'
  return (
    <div className="import-banner">
      <div className="import-banner-content">
        <div className="import-banner-icon">
          <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="spin">
            <circle cx="12" cy="12" r="10" strokeOpacity="0.25"/>
            <path d="M12 2a10 10 0 0 1 10 10" strokeLinecap="round"/>
          </svg>
        </div>
        <div className="import-banner-text">
          <div className="import-banner-title">Importing your financial data…</div>
          <div className="import-banner-msg">{msg}</div>
        </div>
        <div className="import-banner-bar">
          <div className="import-banner-progress" style={{width: pct + '%'}}/>
        </div>
        <div className="import-banner-pct">{pct}%</div>
      </div>
    </div>
  )
}

// ───── App Shell ─────
export default function App() {
  const [route, setRoute] = useState(() => localStorage.getItem('budget-route') || '/')
  // Default to year-to-date, which for 2026 resolves to Jan 1 2026 -> today via
  // resolvePeriod(): the window the owner asked the app to focus on. 'All Time'
  // still reaches the pre-2026 CSV history.
  const [periodId, setPeriodId] = useState(() => localStorage.getItem('budget-period') || 'ytd')
  const [sidebarCollapsed, setSidebarCollapsed] = useState(
    () => localStorage.getItem('budget-sidebar-collapsed') === '1')
  const [categoryColors, setCategoryColors] = useState({})
  const [palette, setPalette] = useState([])
  const reloadColors = useCallback(() => {
    apiFetch('/categories/colors')
      .then(d => { setCategoryColors(d.colors || {}); setPalette(d.palette || []) })
      .catch(() => {})
  }, [])
  useEffect(() => { reloadColors() }, [reloadColors])
  const [importStatus, setImportStatus] = useState(null)
  const [importDone, setImportDone] = useState(false)
  // Monotonic counter that pages include in their fetch deps. We bump it
  // whenever a chat-correct mutates DB rows so every dependent page re-fetches.
  const [dataVersion, setDataVersion] = useState(0)
  const bumpDataVersion = useCallback(() => setDataVersion(v => v + 1), [])
  const appContextValue = useMemo(
    () => ({ dataVersion, bumpDataVersion, categoryColors, palette, reloadColors }),
    [dataVersion, bumpDataVersion, categoryColors, palette, reloadColors]
  )

  useEffect(() => {
    let cancelled = false
    async function pollImport() {
      while (!cancelled) {
        try {
          const res = await fetch('/api/import/status')
          const data = await res.json()
          if (!cancelled) {
            setImportStatus(data)
            if (data.state === 'complete' || data.state === 'idle' || data.state === 'error') {
              setImportDone(true)
              return
            }
          }
        } catch {
          // Backend may not be ready yet — keep polling
        }
        await new Promise(r => setTimeout(r, 2000))
      }
    }
    pollImport()
    return () => { cancelled = true }
  }, [])

  useEffect(() => localStorage.setItem('budget-route', route), [route])
  useEffect(() => localStorage.setItem('budget-period', periodId), [periodId])
  useEffect(() => localStorage.setItem('budget-sidebar-collapsed', sidebarCollapsed ? '1' : '0'),
    [sidebarCollapsed])

  if (!importDone) {
    return <ImportBanner status={importStatus} />
  }

  const navItems = [
    { id: '/', label: 'Dashboard', icon: 'LayoutDashboard' },
    { id: '/txns', label: 'Transactions', icon: 'Receipt' },
    { id: '/income', label: 'Income', icon: 'TrendingUp' },
    { id: '/expenses', label: 'Expenses', icon: 'PieChart' },
    { id: '/refunded', label: 'Refunded', icon: 'Repeat' },
    { id: '/excluded', label: 'Excluded', icon: 'X' },
    { id: '/mortgage', label: 'Mortgage', icon: 'Building' },
    { id: '/activity', label: 'Activity', icon: 'Activity' },
    // Net Worth is retired from the nav (owner directive, 2026-07-24): its
    // figures were persistently wrong and the app is scoped to expenditures.
    // The route and its API remain so old links and tests don't break.
    { id: '/compare', label: 'Compare', icon: 'GitMerge' },
    { id: '/trends', label: 'Trends', icon: 'TrendingUp' },
    { id: '/budgets', label: 'Budgets', icon: 'Target' },
    // Label matches the page title ('Budget Context'); the nav used to say just
    // 'Context' while the page called itself something else.
    { id: '/context', label: 'Budget Context', icon: 'FileText' },
    { id: '/chat', label: 'Chat Correct', icon: 'MessageSquare' },
    { id: '/accounts', label: 'Accounts & Sync', icon: 'Wallet' },
  ]

  let pageTitle = ''
  let pageContent

  // Parse query params from route (e.g. /txns?category=...)
  const routeParts = route.split('?')
  // /accounts.html was the standalone page the Accounts & Sync route replaced.
  // Aliasing it here means a stale stored route or bookmark lands on the real
  // page with the nav item correctly highlighted.
  const baseRoute = ROUTE_ALIASES[routeParts[0]] || routeParts[0]
  const routeParams = new URLSearchParams(routeParts[1] || '')

  function handleNavigate(path) { setRoute(path) }

  // Only routes whose data actually moves with the window get the topbar period
  // control; showing it elsewhere would be a stale, inert dial. Mortgage reads a
  // fixed amortization schedule, Compare picks its own two periods, and Activity /
  // Chat / Context / Accounts / Net Worth are period-free by construction.
  const showPeriod = PERIOD_ROUTES.has(baseRoute)

  switch (baseRoute) {
    case '/':
      pageTitle = 'Dashboard'
      pageContent = <Dashboard periodId={periodId} onNavigate={handleNavigate} />
      break
    case '/mortgage':
      pageTitle = 'Mortgage'
      pageContent = <MortgagePage />
      break
    case '/networth':
      pageTitle = 'Net Worth'
      pageContent = <NetWorthPage />
      break
    case '/budgets':
      pageTitle = 'Budgets'
      pageContent = <BudgetsPage periodId={periodId} />
      break
    case '/txns':
      pageTitle = 'Transactions'
      pageContent = <TransactionsPage periodId={periodId} key={route} initialCategory={routeParams.get('category')} />
      break
    case '/income':
      pageTitle = 'Income'
      pageContent = <FlowPage flow="income" periodId={periodId} />
      break
    case '/expenses':
      pageTitle = 'Expenses'
      pageContent = <FlowPage flow="expense" periodId={periodId} />
      break
    case '/refunded':
      pageTitle = 'Refunded'
      pageContent = <RefundedPage periodId={periodId} />
      break
    case '/excluded':
      pageTitle = 'Excluded'
      pageContent = <ExcludedPage periodId={periodId} />
      break
    case '/compare':
      pageTitle = 'Compare'
      pageContent = <ComparePage periodId={periodId} />
      break
    case '/trends':
      pageTitle = 'Trends'
      pageContent = <TrendsPage periodId={periodId} />
      break
    case '/activity':
      pageTitle = 'Activity'
      pageContent = <ActivityPage />
      break
    case '/chat':
      pageTitle = 'Chat Correct'
      pageContent = <ChatPage />
      break
    case '/context':
      pageTitle = 'Budget Context'
      pageContent = <ContextPage />
      break
    case '/accounts':
      pageTitle = 'Accounts & Sync'
      pageContent = <AccountsPage />
      break
    default:
      pageTitle = 'Dashboard'
      pageContent = <Dashboard periodId={periodId} />
  }

  return (
    <AppContext.Provider value={appContextValue}>
      <div className={`app dark ${sidebarCollapsed ? 'is-collapsed' : ''}`}>
        <aside className="sb">
          <div className="sb-brand">
            <img className="sb-logo" src="/logo.png" width={36} height={36} alt="Balance" />
            <div className="sb-brandwrap">
              <span className="sb-brandname">Balance</span>
              <span className="sb-brandtag">Personal Finance</span>
            </div>
          </div>
          <div className="sb-section">
            <div className="sb-section-label">Navigation</div>
            {navItems.map(item => (
              <button
                key={item.id}
                // Match on baseRoute: /txns?category=Groceries is still Transactions,
                // and used to leave every nav item unhighlighted.
                className={`sb-item ${baseRoute === item.id ? 'is-active' : ''}`}
                title={item.label}
                onClick={() => setRoute(item.id)}
              >
                <Icon name={item.icon} size={16} /> <span className="sb-label">{item.label}</span>
              </button>
            ))}
          </div>
        </aside>

        <div className="main">
          <header className="topbar">
            <button className="iconbtn" onClick={() => setSidebarCollapsed(c => !c)}
                    title={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
                    aria-label={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
                    aria-pressed={sidebarCollapsed} data-testid="sidebar-toggle">
              <Icon name="PanelLeft" size={17} />
            </button>
            <div className="topbar-title" data-testid="page-title">{pageTitle}</div>
            {showPeriod && <>
              <div className="topbar-sep" />
              <PeriodControl value={periodId} onChange={setPeriodId} />
            </>}
            <div className="topbar-spacer" />
          </header>
          <main className="content">{pageContent}</main>
        </div>
        <FloatingChatWidget />
      </div>
    </AppContext.Provider>
  )
}
