// Trends — income / expense / net over time as bar+line chart, plus a stacked area by category.

function Trends({ filter, setFilter }) {
  const [mode, setMode] = useState("monthly"); // monthly | quarterly
  const [metric, setMetric] = useState("net"); // net | income | expense
  const [stackBy, setStackBy] = useState("cat"); // cat | sub

  // Group txns into months
  const series = useMemo(() => {
    // Build 14 months
    const months = [];
    for (let i = 13; i >= 0; i--) {
      const d = startOfMonth(addMonths(TODAY, -i));
      months.push({ date: d, income: 0, expense: 0, cats: {} });
    }
    for (const t of TXNS) {
      const idx = months.findIndex(m => t.date.getFullYear() === m.date.getFullYear() && t.date.getMonth() === m.date.getMonth());
      if (idx < 0) continue;
      const b = months[idx];
      if (t.flow === "income") b.income += t.amount;
      else b.expense += t.amount;
      const key = stackBy === "cat" ? t.catId : t.subId;
      if (t.flow === "expense") {
        b.cats[key] = (b.cats[key] || 0) + t.amount;
      }
    }
    return months;
  }, [stackBy]);

  // Pick top 6 expense categories overall
  const topCats = useMemo(() => {
    const m = new Map();
    for (const b of series) for (const [k, v] of Object.entries(b.cats)) m.set(k, (m.get(k) || 0) + v);
    return [...m.entries()].sort((a, b) => b[1] - a[1]).slice(0, 6).map(([k]) => k);
  }, [series]);

  return (
    <div className="page">
      <div>
        <h1 className="h1">Trends</h1>
        <div className="subtitle">Last 14 months of cash flow and spend by category</div>
      </div>

      <div className="card">
        <div className="section-head">
          <div>
            <div className="h">Income vs Expense</div>
            <div className="sub">Monthly totals, net cash flow overlaid</div>
          </div>
          <div className="seg">
            {["monthly", "quarterly"].map(m => (
              <button key={m} className={`seg-item ${mode === m ? "is-active" : ""}`} onClick={() => setMode(m)}>{m[0].toUpperCase() + m.slice(1)}</button>
            ))}
          </div>
        </div>
        <div style={{padding:"12px 20px 20px"}}>
          <IncomeExpenseBars series={series}/>
        </div>
      </div>

      <div className="card">
        <div className="section-head">
          <div>
            <div className="h">Expense Mix Over Time</div>
            <div className="sub">Stacked spending by {stackBy === "cat" ? "category" : "subcategory"}</div>
          </div>
          <div className="filter-row">
            <span className="label">Stack by</span>
            <div className="seg">
              {["cat", "sub"].map(s => (
                <button key={s} className={`seg-item ${stackBy === s ? "is-active" : ""}`} onClick={() => setStackBy(s)}>
                  {s === "cat" ? "Category" : "Subcategory"}
                </button>
              ))}
            </div>
          </div>
        </div>
        <div style={{padding:"12px 20px 20px"}}>
          <StackedBars series={series} keys={topCats} stackBy={stackBy} onPick={(k) => setFilter(stackBy === "cat" ? { catId: k } : { subId: k })}/>
        </div>
      </div>

      <div className="card">
        <div className="section-head">
          <div>
            <div className="h">Net Worth Trajectory</div>
            <div className="sub">Past 24 months</div>
          </div>
        </div>
        <div style={{padding:"12px 20px 20px"}}>
          <AreaChart values={NW_HISTORY.map(p => ({ x: p.date, y: p.value }))} color="#4ade80" height={220}/>
        </div>
      </div>
    </div>
  );
}

function IncomeExpenseBars({ series }) {
  const ref = useRef(null);
  const [w, setW] = useState(900);
  const [hov, setHov] = useState(null);
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(es => { for (const e of es) setW(e.contentRect.width); });
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  const height = 280;
  const pad = { l: 56, r: 16, t: 12, b: 28 };
  const innerW = w - pad.l - pad.r;
  const innerH = height - pad.t - pad.b;
  const maxV = Math.max(...series.flatMap(s => [s.income, s.expense]));
  const yMap = v => pad.t + (1 - v / maxV) * innerH;
  const groupW = innerW / series.length;
  const barW = (groupW - 8) / 2;

  const netVals = series.map(s => s.income - s.expense);
  const netYmap = v => yMap(v);

  const ticks = 4;
  const yTicks = Array.from({length: ticks + 1}, (_, i) => (maxV * i) / ticks);

  return (
    <div ref={ref} style={{position:"relative"}}>
      <svg className="bar-chart" viewBox={`0 0 ${w} ${height}`} height={height}>
        {yTicks.map((t, i) => (
          <g key={i}>
            <line className="chart-grid" x1={pad.l} x2={w - pad.r} y1={yMap(t)} y2={yMap(t)}/>
            <text className="chart-label" x={pad.l - 8} y={yMap(t)} textAnchor="end" dominantBaseline="central">{fmtCurrency(t, {compact:true})}</text>
          </g>
        ))}
        {series.map((s, i) => {
          const gx = pad.l + i * groupW + 4;
          return (
            <g key={i} onMouseEnter={(e) => setHov({ i, x: e.clientX, y: e.clientY })} onMouseLeave={() => setHov(null)}>
              <rect x={gx} y={yMap(s.income)} width={barW} height={Math.max(1, innerH - yMap(s.income) + pad.t)} fill="#22c55e" rx={2}/>
              <rect x={gx + barW + 4} y={yMap(s.expense)} width={barW} height={Math.max(1, innerH - yMap(s.expense) + pad.t)} fill="#f43f5e" rx={2}/>
              <text className="chart-label" x={gx + barW + 2} y={pad.t + innerH + 18} textAnchor="middle">{fmtMonth(s.date).split(" ")[0]}</text>
            </g>
          );
        })}
        {/* net line */}
        <path
          d={series.map((s, i) => {
            const x = pad.l + i * groupW + groupW / 2;
            const y = netYmap(s.income - s.expense);
            return (i === 0 ? "M" : "L") + x.toFixed(1) + "," + y.toFixed(1);
          }).join(" ")}
          fill="none" stroke="#a78bfa" strokeWidth={2}/>
        {series.map((s, i) => (
          <circle key={i} cx={pad.l + i * groupW + groupW / 2} cy={netYmap(s.income - s.expense)} r={3} fill="#a78bfa" stroke="var(--card)" strokeWidth={1.5}/>
        ))}
      </svg>
      {/* Legend */}
      <div style={{display:"flex", gap:14, fontSize:12, color:"var(--muted-foreground)", padding:"4px 0 0 56px", flexWrap:"wrap"}}>
        <span style={{whiteSpace:"nowrap"}}><span className="swatch" style={{background:"#22c55e"}}/> Income</span>
        <span style={{whiteSpace:"nowrap"}}><span className="swatch" style={{background:"#f43f5e"}}/> Expense</span>
        <span style={{whiteSpace:"nowrap"}}><span className="swatch" style={{background:"#a78bfa", borderRadius:9999}}/> Net</span>
      </div>
    </div>
  );
}

function StackedBars({ series, keys, stackBy, onPick }) {
  const ref = useRef(null);
  const [w, setW] = useState(900);
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(es => { for (const e of es) setW(e.contentRect.width); });
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  const height = 280;
  const pad = { l: 56, r: 16, t: 12, b: 28 };
  const innerW = w - pad.l - pad.r;
  const innerH = height - pad.t - pad.b;
  const maxV = Math.max(...series.map(s => Object.values(s.cats).reduce((a, b) => a + b, 0)));
  const yMap = v => pad.t + (1 - v / maxV) * innerH;
  const groupW = innerW / series.length;
  const barW = groupW - 6;

  const colorOf = (k) => {
    if (stackBy === "cat") return CATEGORIES.expense[k]?.color || "#94a3b8";
    const lk = categoryLookup(k); return lk?.subColor || "#94a3b8";
  };
  const labelOf = (k) => {
    if (stackBy === "cat") return CATEGORIES.expense[k]?.name || k;
    const lk = categoryLookup(k); return lk?.subName || k;
  };

  const ticks = 4;
  const yTicks = Array.from({length: ticks + 1}, (_, i) => (maxV * i) / ticks);

  return (
    <div ref={ref} style={{position:"relative"}}>
      <svg className="bar-chart" viewBox={`0 0 ${w} ${height}`} height={height}>
        {yTicks.map((t, i) => (
          <g key={i}>
            <line className="chart-grid" x1={pad.l} x2={w - pad.r} y1={yMap(t)} y2={yMap(t)}/>
            <text className="chart-label" x={pad.l - 8} y={yMap(t)} textAnchor="end" dominantBaseline="central">{fmtCurrency(t, {compact:true})}</text>
          </g>
        ))}
        {series.map((s, i) => {
          const gx = pad.l + i * groupW + 3;
          let yCursor = pad.t + innerH;
          const cats = Object.entries(s.cats).sort((a, b) => b[1] - a[1]);
          return (
            <g key={i}>
              {cats.map(([k, v], j) => {
                const h = (v / maxV) * innerH;
                yCursor -= h;
                return <rect key={k} x={gx} y={yCursor} width={barW} height={h} fill={colorOf(k)} opacity={keys.includes(k) ? 1 : 0.5}
                              onClick={() => onPick(k)} style={{cursor:"pointer"}}>
                  <title>{labelOf(k)}: {fmtCurrency(v)}</title>
                </rect>;
              })}
              <text className="chart-label" x={gx + barW / 2} y={pad.t + innerH + 18} textAnchor="middle">{fmtMonth(s.date).split(" ")[0]}</text>
            </g>
          );
        })}
      </svg>
      <div style={{display:"flex", gap:14, fontSize:12, color:"var(--muted-foreground)", padding:"4px 0 0 56px", flexWrap:"wrap"}}>
        {keys.map(k => (
          <span key={k} onClick={() => onPick(k)} style={{cursor:"pointer", whiteSpace:"nowrap"}}>
            <span className="swatch" style={{background: colorOf(k)}}/> {labelOf(k)}
          </span>
        ))}
      </div>
    </div>
  );
}

window.Trends = Trends;
