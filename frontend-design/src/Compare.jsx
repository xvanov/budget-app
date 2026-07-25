// Compare two time periods — diff view with delta and % change per category.

function Compare() {
  const [periodA, setPeriodA] = useState("this-month");
  const [periodB, setPeriodB] = useState("last-month");
  const A = useMemo(() => resolvePeriod(periodA), [periodA]);
  const B = useMemo(() => resolvePeriod(periodB), [periodB]);

  const aTxns = useMemo(() => txnsInPeriod(A), [A]);
  const bTxns = useMemo(() => txnsInPeriod(B), [B]);
  const aSum = summarize(aTxns), bSum = summarize(bTxns);

  // Compute per-category for both
  const byCat = useMemo(() => {
    const a = groupByCat(aTxns), b = groupByCat(bTxns);
    const keys = new Set([...a.keys(), ...b.keys()]);
    const rows = [];
    for (const k of keys) {
      const aV = a.get(k)?.total || 0, bV = b.get(k)?.total || 0;
      const flow = (a.get(k) || b.get(k)).flow;
      rows.push({ catId: k, flow, a: aV, b: bV, delta: aV - bV, pct: bV === 0 ? 0 : ((aV - bV) / bV) * 100 });
    }
    return rows.sort((a, b) => Math.abs(b.delta) - Math.abs(a.delta));
  }, [aTxns, bTxns]);

  const expRows = byCat.filter(r => r.flow === "expense");
  const incRows = byCat.filter(r => r.flow === "income");

  return (
    <div className="page">
      <div>
        <h1 className="h1">Compare</h1>
        <div className="subtitle">Side-by-side diff of any two periods</div>
      </div>

      <div className="compare-grid">
        <div>
          <div className="compare-period">
            <span className="swatch" style={{background:"var(--primary)"}}/>
            <span className="label">Period A</span>
            <span className="val mono">{A.label}</span>
          </div>
          <div style={{padding:"8px 0"}}>
            <PeriodPickerCompact value={periodA} onChange={setPeriodA}/>
          </div>
          <div className="card" style={{padding:"16px 20px"}}>
            <div className="kpi-label">A · Net Cash Flow</div>
            <div className={`kpi-value ${aSum.net >= 0 ? "pos" : "neg"}`}>{fmtCurrency(aSum.net)}</div>
            <div className="kpi-delta" style={{marginTop:8}}>
              <span style={{color:"#22c55e"}}>Income {fmtCurrency(aSum.income)}</span>
              <span style={{color:"var(--destructive)"}}>Expense {fmtCurrency(aSum.expense)}</span>
            </div>
          </div>
        </div>

        <div>
          <div className="compare-period" style={{background:"color-mix(in oklab, var(--accent-orange) 14%, transparent)"}}>
            <span className="swatch" style={{background:"var(--accent-orange)"}}/>
            <span className="label">Period B</span>
            <span className="val mono">{B.label}</span>
          </div>
          <div style={{padding:"8px 0"}}>
            <PeriodPickerCompact value={periodB} onChange={setPeriodB}/>
          </div>
          <div className="card" style={{padding:"16px 20px"}}>
            <div className="kpi-label">B · Net Cash Flow</div>
            <div className={`kpi-value ${bSum.net >= 0 ? "pos" : "neg"}`}>{fmtCurrency(bSum.net)}</div>
            <div className="kpi-delta" style={{marginTop:8}}>
              <span style={{color:"#22c55e"}}>Income {fmtCurrency(bSum.income)}</span>
              <span style={{color:"var(--destructive)"}}>Expense {fmtCurrency(bSum.expense)}</span>
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
            <span><span className="swatch" style={{background:"var(--primary)"}}/> A: {A.label}</span>
            <span style={{marginLeft:8}}><span className="swatch" style={{background:"var(--accent-orange)"}}/> B: {B.label}</span>
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
              <span/>
              <span/>
              <span/>
              <span/>
            </div>
            {expRows.map(r => <DiffRow key={r.catId} row={r} flow="expense"/>)}
            <div className="diff-row" style={{background:"color-mix(in oklab, var(--secondary) 50%, transparent)", cursor:"default", marginTop: 4}}>
              <span/>
              <span style={{fontWeight:600, fontSize:11, textTransform:"uppercase", letterSpacing:"0.06em", color:"var(--muted-foreground)"}}>Income</span>
              <span/>
              <span/>
              <span/>
              <span/>
            </div>
            {incRows.map(r => <DiffRow key={r.catId} row={r} flow="income"/>)}
          </div>
        </div>
      </div>
    </div>
  );
}

function DiffRow({ row, flow }) {
  const meta = CATEGORIES[flow][row.catId];
  // For expenses, "up" (more spent) is bad → red. For income, "up" is good → green.
  const better = flow === "expense" ? row.delta < 0 : row.delta > 0;
  const deltaClass = row.delta === 0 ? "" : (better ? "down" : "up");
  const max = Math.max(row.a, row.b) || 1;
  return (
    <div className="diff-row">
      <span className="swatch" style={{background: meta.color}}/>
      <div>
        <div style={{fontWeight:500}}>{meta.name}</div>
        <div style={{position:"relative", height:14, marginTop:4}}>
          <div style={{position:"absolute", left:0, top:0, height:5, width: (row.a / max) * 100 + "%", background:"var(--primary)", borderRadius:3}}/>
          <div style={{position:"absolute", left:0, top:8, height:5, width: (row.b / max) * 100 + "%", background:"var(--accent-orange)", borderRadius:3}}/>
        </div>
      </div>
      <span className="num mono">{fmtCurrency(row.a, {compact:true})}</span>
      <span className="num mono">{fmtCurrency(row.b, {compact:true})}</span>
      <span className={"num mono delta " + deltaClass}>{(row.delta >= 0 ? "+" : "−") + fmtCurrency(Math.abs(row.delta), {compact:true})}</span>
      <span className={"num mono delta " + deltaClass}>{row.b === 0 ? "—" : fmtPct(row.pct, 0)}</span>
    </div>
  );
}

function PeriodPickerCompact({ value, onChange }) {
  return (
    <div className="seg" style={{width:"100%", flexWrap:"wrap"}}>
      {PERIOD_PRESETS.map(p => (
        <button key={p.id} className={`seg-item ${value === p.id ? "is-active" : ""}`} onClick={() => onChange(p.id)}>{p.label}</button>
      ))}
    </div>
  );
}

window.Compare = Compare;
