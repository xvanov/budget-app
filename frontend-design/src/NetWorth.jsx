// Net Worth: treemap + grouped account list + history chart.

function NetWorth() {
  // Compute assets/liabilities
  const data = useMemo(() => {
    const groups = {
      cash: { label: "Cash & Checking", accounts: [], total: 0, color: "#38bdf8" },
      savings: { label: "Savings", accounts: [], total: 0, color: "#22d3ee" },
      brokerage: { label: "Brokerage", accounts: [], total: 0, color: "#a78bfa" },
      retirement: { label: "Retirement", accounts: [], total: 0, color: "#c4b5fd" },
      real_estate: { label: "Real Estate", accounts: [], total: 0, color: "#10b981" },
      credit: { label: "Credit Cards", accounts: [], total: 0, color: "#f43f5e" },
    };
    let assets = 0, liabilities = 0;
    for (const a of ACCOUNTS) {
      const g = groups[a.kind] || groups.cash;
      let val = a.balance;
      if (a.kind === "real_estate") {
        val = a.balance - (a.debt || 0); // equity
        liabilities += (a.debt || 0);
        assets += a.balance;
      } else if (a.kind === "credit") {
        liabilities += Math.abs(a.balance);
      } else {
        assets += a.balance;
      }
      g.accounts.push({ ...a, displayValue: val });
      g.total += val;
    }
    const total = Object.values(groups).reduce((s, g) => s + g.total, 0);
    return { groups, total, assets, liabilities };
  }, []);

  return (
    <div className="page">
      <div>
        <h1 className="h1">Net Worth</h1>
        <div className="subtitle">Snapshot across {ACCOUNTS.length} accounts and properties · updated {fmtDateLong(TODAY)}</div>
      </div>

      <div className="kpi-grid" style={{gridTemplateColumns:"repeat(3, 1fr)"}}>
        <KpiCard label="Total Net Worth" value={fmtCurrency(data.total)}
                 valueClass="pos" delta={4.8} deltaLabel="last 30 days"
                 sparkValues={NW_HISTORY.map(p => p.value)} color="#4ade80"/>
        <KpiCard label="Assets" value={fmtCurrency(data.assets)}
                 sub={<span>Across 6 categories</span>} color="#a78bfa"/>
        <KpiCard label="Liabilities" value={fmtCurrency(data.liabilities)}
                 sub={<span><Icon name="CircleAlert" size={12}/> 8 mortgages · 2 cards</span>}
                 color="#f43f5e"/>
      </div>

      <div className="card">
        <div className="section-head">
          <div>
            <div className="h">Net Worth Over Time</div>
            <div className="sub">Last 24 months</div>
          </div>
          <div className="seg">
            <button className="seg-item is-active">All</button>
            <button className="seg-item">Assets</button>
            <button className="seg-item">Liabilities</button>
          </div>
        </div>
        <div style={{padding:"12px 20px 20px"}}>
          <AreaChart values={NW_HISTORY.map(p => ({x: p.date, y: p.value}))} color="#4ade80" height={220}/>
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
          <Treemap groups={data.groups} total={data.assets}/>
        </div>

        <div className="card">
          <div className="section-head">
            <div>
              <div className="h">Accounts</div>
              <div className="sub">Click any account to drill in</div>
            </div>
            <button className="btn btn--ghost btn--sm"><Icon name="Plus" size={12}/> Link account</button>
          </div>
          <div className="nw-list">
            {Object.entries(data.groups).map(([gid, g]) => (
              <React.Fragment key={gid}>
                <div className="nw-grouptitle">
                  <div style={{display:"flex", alignItems:"center", gap:8}}>
                    <span className="swatch" style={{background: g.color}}/>
                    <span className="nw-grouptitle-name">{g.label}</span>
                  </div>
                  <div className="nw-grouptitle-amt mono">{fmtCurrency(g.total, { compact: true })}</div>
                </div>
                {g.accounts.map(a => {
                  // deterministic mock 30d change
                  let h = 0; for (let i = 0; i < a.id.length; i++) h = (h * 31 + a.id.charCodeAt(i)) | 0;
                  const dpct = ((h % 700) / 100) - 1.5; // -1.5% .. +5.5%
                  return (
                    <div className="nw-list-row" key={a.id}>
                      <span className="dot" style={{background: g.color}}/>
                      <div>
                        <div className="nw-list-name">{a.name}</div>
                        {a.debt ? <div className="nw-list-sub">Value {fmtCurrency(a.balance, {compact:true})} · Loan {fmtCurrency(a.debt, {compact:true})}</div> : <div className="nw-list-sub">{a.kind.replace("_", " ")}</div>}
                      </div>
                      <div className="nw-list-amt mono">{fmtCurrency(a.displayValue, { compact: true })}</div>
                      <div className={`nw-list-delta ${dpct >= 0 ? "up" : "down"}`}>{fmtPct(dpct)}</div>
                    </div>
                  );
                })}
              </React.Fragment>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

// Squarified treemap (small, good-enough)
function Treemap({ groups, total }) {
  const ref = useRef(null);
  const [w, setW] = useState(560);
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(es => { for (const e of es) setW(e.contentRect.width); });
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  const h = 360;

  // Compute layout: flatten all accounts with positive value
  const items = useMemo(() => {
    const rows = [];
    for (const [gid, g] of Object.entries(groups)) {
      for (const a of g.accounts) {
        if (a.displayValue > 0) rows.push({ id: a.id, name: a.name, value: a.displayValue, color: g.color, group: g.label });
      }
    }
    rows.sort((a, b) => b.value - a.value);
    return rows;
  }, [groups]);

  const layout = useMemo(() => slice_and_dice(items, w, h), [items, w, h]);

  return (
    <div ref={ref} style={{padding:"4px 20px 20px"}}>
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
  );
}

function slice_and_dice(items, W, H) {
  // Simple recursive split — alternate horizontal/vertical based on aspect.
  const total = items.reduce((s, it) => s + it.value, 0);
  const out = [];
  function rec(arr, x, y, w, h) {
    const sum = arr.reduce((s, it) => s + it.value, 0);
    if (arr.length === 1) { out.push({ ...arr[0], x, y, w, h }); return; }
    // Take items in order, fill the shorter axis first
    const horizontal = w > h;
    const len = horizontal ? w : h;
    // pick a split point ~ first item until we cover ~ half (or proportional)
    let acc = 0, k = 0;
    for (; k < arr.length - 1; k++) {
      acc += arr[k].value;
      if (acc >= sum * 0.5) { k++; break; }
    }
    if (k === 0) k = 1;
    const left = arr.slice(0, k), right = arr.slice(k);
    const leftSum = left.reduce((s, it) => s + it.value, 0);
    const split = (leftSum / sum) * len;
    if (horizontal) {
      rec(left, x, y, split, h);
      rec(right, x + split, y, w - split, h);
    } else {
      rec(left, x, y, w, split);
      rec(right, x, y + split, w, h - split);
    }
  }
  rec(items, 0, 0, W, H);
  return out;
}

// ───── Generic area chart used in NetWorth + Trends ─────
function AreaChart({ values, color = "var(--primary)", height = 200, currency = true }) {
  const ref = useRef(null);
  const [w, setW] = useState(800);
  const [hov, setHov] = useState(null);
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(es => { for (const e of es) setW(e.contentRect.width); });
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  const pad = { l: 56, r: 16, t: 12, b: 28 };
  const innerW = w - pad.l - pad.r;
  const innerH = height - pad.t - pad.b;
  const ys = values.map(v => v.y);
  const min = Math.min(...ys), max = Math.max(...ys);
  const yRange = max - min || 1;
  const yMin = min - yRange * 0.08, yMax = max + yRange * 0.08;
  const xs = values.map((_, i) => pad.l + (i / (values.length - 1)) * innerW);
  const yMap = v => pad.t + (1 - (v - yMin) / (yMax - yMin)) * innerH;
  const ysPx = ys.map(yMap);
  const d = xs.map((x, i) => (i === 0 ? "M" : "L") + x.toFixed(1) + "," + ysPx[i].toFixed(1)).join(" ");
  const dFill = d + ` L${xs.at(-1)},${pad.t + innerH} L${xs[0]},${pad.t + innerH} Z`;
  // y ticks
  const ticks = 4;
  const yTicks = Array.from({length: ticks + 1}, (_, i) => yMin + ((yMax - yMin) * i / ticks));

  return (
    <div ref={ref} style={{position:"relative"}}>
      <svg className="area-chart" viewBox={`0 0 ${w} ${height}`} height={height}
        onMouseMove={e => {
          const rect = ref.current.querySelector("svg").getBoundingClientRect();
          const xPx = e.clientX - rect.left;
          const idx = Math.max(0, Math.min(values.length - 1, Math.round((xPx - pad.l) / innerW * (values.length - 1))));
          setHov({ idx, x: xs[idx], y: ysPx[idx], v: values[idx] });
        }}
        onMouseLeave={() => setHov(null)}>
        <defs>
          <linearGradient id="areaGrad" x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity="0.35"/>
            <stop offset="100%" stopColor={color} stopOpacity="0"/>
          </linearGradient>
        </defs>
        {/* gridlines */}
        {yTicks.map((t, i) => (
          <g key={i}>
            <line className="chart-grid" x1={pad.l} x2={w - pad.r} y1={yMap(t)} y2={yMap(t)}/>
            <text className="chart-label" x={pad.l - 8} y={yMap(t)} textAnchor="end" dominantBaseline="central">{currency ? fmtCurrency(t, {compact:true}) : Math.round(t)}</text>
          </g>
        ))}
        {/* x labels */}
        {[0, Math.floor(values.length/4), Math.floor(values.length/2), Math.floor(values.length*3/4), values.length-1].map(i => (
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
        <div className="tooltip" style={{left: hov.x + 12, top: hov.y + 12, position:"absolute"}}>
          <div className="t-label">{hov.v.x instanceof Date ? fmtMonth(hov.v.x) : hov.v.x}</div>
          <div className="t-value">{currency ? fmtCurrency(hov.v.y) : hov.v.y}</div>
        </div>
      )}
    </div>
  );
}

window.NetWorth = NetWorth;
window.AreaChart = AreaChart;
