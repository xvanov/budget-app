// Shared helpers, hooks, primitives unique to the budget app.
// Loaded before view files.

const { useState, useEffect, useMemo, useRef, useCallback, useContext, createContext } = React;
const { TXNS, CATEGORIES, ACCOUNTS, NW_HISTORY, BUDGETS, TODAY, categoryLookup, fmtCurrency, fmtPct } = window.AppData;

// ───── Period helpers ─────
function startOfMonth(d) { return new Date(d.getFullYear(), d.getMonth(), 1); }
function endOfMonth(d) { return new Date(d.getFullYear(), d.getMonth() + 1, 0, 23, 59, 59); }
function addMonths(d, n) { return new Date(d.getFullYear(), d.getMonth() + n, d.getDate()); }
function fmtDate(d) { return d.toLocaleDateString("en-US", { month: "short", day: "numeric" }); }
function fmtDateLong(d) { return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }); }
function fmtMonth(d) { return d.toLocaleDateString("en-US", { month: "short", year: "numeric" }); }
function daysBetween(a, b) { return Math.round((b - a) / 86_400_000); }

const PERIOD_PRESETS = [
  { id: "this-month", label: "This Month" },
  { id: "last-month", label: "Last Month" },
  { id: "last-3", label: "Last 3 Months" },
  { id: "last-6", label: "Last 6 Months" },
  { id: "ytd", label: "Year to Date" },
  { id: "last-12", label: "Last 12 Months" },
  { id: "all", label: "All Time" },
];

function resolvePeriod(id) {
  const now = TODAY;
  switch (id) {
    case "this-month": return { start: startOfMonth(now), end: now, label: fmtMonth(now) };
    case "last-month": { const s = startOfMonth(addMonths(now, -1)); return { start: s, end: endOfMonth(s), label: fmtMonth(s) }; }
    case "last-3": return { start: startOfMonth(addMonths(now, -2)), end: now, label: "Last 3 mo" };
    case "last-6": return { start: startOfMonth(addMonths(now, -5)), end: now, label: "Last 6 mo" };
    case "ytd": return { start: new Date(now.getFullYear(), 0, 1), end: now, label: now.getFullYear() + " YTD" };
    case "last-12": return { start: startOfMonth(addMonths(now, -11)), end: now, label: "Last 12 mo" };
    case "all": return { start: new Date(2020, 0, 1), end: now, label: "All time" };
    default: return resolvePeriod("this-month");
  }
}

// ───── Filtering ─────
function txnsInPeriod(period) {
  return TXNS.filter(t => t.date >= period.start && t.date <= period.end);
}
function txnsWithFilter(period, filter) {
  let arr = txnsInPeriod(period);
  if (filter?.subId) arr = arr.filter(t => t.subId === filter.subId);
  else if (filter?.catId) arr = arr.filter(t => t.catId === filter.catId);
  else if (filter?.account) arr = arr.filter(t => t.account === filter.account);
  return arr;
}

// ───── Aggregations ─────
function summarize(txns) {
  let income = 0, expense = 0;
  for (const t of txns) {
    if (t.flow === "income") income += t.amount;
    else if (t.flow === "expense") expense += t.amount;
  }
  return { income, expense, net: income - expense };
}

function groupBySub(txns) {
  const m = new Map();
  for (const t of txns) {
    const key = t.subId;
    if (!m.has(key)) m.set(key, { subId: key, catId: t.catId, flow: t.flow, total: 0, count: 0 });
    const v = m.get(key); v.total += t.amount; v.count++;
  }
  return m;
}
function groupByCat(txns) {
  const m = new Map();
  for (const t of txns) {
    if (!m.has(t.catId)) m.set(t.catId, { catId: t.catId, flow: t.flow, total: 0, subs: new Map() });
    const c = m.get(t.catId); c.total += t.amount;
    if (!c.subs.has(t.subId)) c.subs.set(t.subId, 0);
    c.subs.set(t.subId, c.subs.get(t.subId) + t.amount);
  }
  return m;
}

// ───── Period controls component ─────
function PeriodControl({ value, onChange, allowCompact = false }) {
  const period = resolvePeriod(value);
  return (
    <div className="period-bar">
      <Icon name="Calendar" size={14}/>
      <div className="seg">
        {PERIOD_PRESETS.map(p => (
          <button key={p.id}
            onClick={() => onChange(p.id)}
            className={`seg-item ${value === p.id ? "is-active" : ""}`}>{p.label}</button>
        ))}
      </div>
      <div className="period-range">
        <span className="mono">{fmtDate(period.start)}</span> → <span className="mono">{fmtDate(period.end)}</span>
      </div>
    </div>
  );
}

// ───── Filter banner (when a category/sub/account filter is active) ─────
function FilterBanner({ filter, onClear }) {
  if (!filter) return null;
  let label = "";
  if (filter.subId) { const c = categoryLookup(filter.subId); label = `${c.catName} → ${c.subName}`; }
  else if (filter.catId) { const flow = ["income","expense"].find(f => CATEGORIES[f][filter.catId]); label = CATEGORIES[flow][filter.catId].name; }
  else if (filter.account) label = ACCOUNTS.find(a => a.id === filter.account)?.name || filter.account;
  return (
    <div className="filter-banner">
      <Icon name="Filter" size={14}/>
      <span className="filter-banner-label">Filtered by</span>
      <span className="filter-banner-value">{label}</span>
      <button className="btn btn--ghost btn--xs" onClick={onClear} style={{marginLeft:"auto"}}>
        <Icon name="X" size={12}/> Clear
      </button>
    </div>
  );
}

// ───── Sparkline ─────
function Sparkline({ values, color = "var(--primary)", height = 28, fill = true }) {
  const pad = 2;
  const w = 100, h = height;
  const max = Math.max(...values), min = Math.min(...values);
  const range = max - min || 1;
  const pts = values.map((v, i) => {
    const x = pad + (i / (values.length - 1)) * (w - pad * 2);
    const y = pad + (1 - (v - min) / range) * (h - pad * 2);
    return [x, y];
  });
  const d = pts.map((p, i) => (i === 0 ? "M" : "L") + p[0].toFixed(1) + "," + p[1].toFixed(1)).join(" ");
  const d2 = d + ` L${pts.at(-1)[0]},${h} L${pts[0][0]},${h} Z`;
  return (
    <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" className="kpi-spark">
      {fill && <path d={d2} fill={color} opacity={0.2}/>}
      <path d={d} fill="none" stroke={color} strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round"/>
    </svg>
  );
}

// ───── Cat dot with color ─────
function CatDot({ subId, catId, size = 8 }) {
  let color;
  if (subId) { const c = categoryLookup(subId); color = c?.subColor; }
  if (!color && catId) { const flow = ["income","expense"].find(f => CATEGORIES[f][catId]); color = CATEGORIES[flow]?.[catId]?.color; }
  if (!color) color = "var(--muted-foreground)";
  return <span className="dot" style={{ width: size, height: size, background: color }}/>;
}

// ───── Click-outside hook ─────
function useClickOutside(onClose) {
  const ref = useRef(null);
  useEffect(() => {
    function h(e) { if (ref.current && !ref.current.contains(e.target)) onClose(); }
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, [onClose]);
  return ref;
}

// ───── Category picker popover (for recategorize) ─────
function CategoryPicker({ anchor, onPick, onClose, flow = "expense" }) {
  const ref = useClickOutside(onClose);
  const tree = CATEGORIES[flow];
  return (
    <div ref={ref} className="popover" style={{ position: "absolute", top: anchor?.y, left: anchor?.x }}>
      <div className="popover-section-label">Re-categorize</div>
      {Object.entries(tree).map(([catId, cat]) => (
        <React.Fragment key={catId}>
          <div className="popover-item" style={{opacity:0.7}}><span className="swatch" style={{background: cat.color}}/> {cat.name}</div>
          {Object.entries(cat.subs).map(([subId, sub]) => (
            <div key={subId} className="popover-item" style={{paddingLeft: 28}} onClick={() => onPick(subId)}>
              <span className="dot" style={{background: sub.color}}/> {sub.name}
            </div>
          ))}
        </React.Fragment>
      ))}
    </div>
  );
}

Object.assign(window, {
  startOfMonth, endOfMonth, addMonths, fmtDate, fmtDateLong, fmtMonth, daysBetween,
  PERIOD_PRESETS, resolvePeriod, txnsInPeriod, txnsWithFilter,
  summarize, groupBySub, groupByCat,
  PeriodControl, FilterBanner, Sparkline, CatDot, CategoryPicker, useClickOutside,
});
