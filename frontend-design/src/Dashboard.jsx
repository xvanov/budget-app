// Dashboard: KPIs, Sankey, recent transactions.

function KpiCard({ label, value, sub, delta, deltaLabel, sparkValues, color = "var(--primary)", icon, valueClass = "" }) {
  let deltaStr = null;
  if (delta != null && isFinite(delta)) {
    const abs = Math.abs(delta);
    if (abs > 999) deltaStr = (delta >= 0 ? "+" : "−") + ">999%";
    else deltaStr = fmtPct(delta, abs >= 100 ? 0 : 1);
  }
  return (
    <div className="card kpi">
      <div className="kpi-label">{label}</div>
      <div className={"kpi-value " + valueClass}>{value}</div>
      {sub && <div className="kpi-delta" style={{whiteSpace:"nowrap"}}>{sub}</div>}
      {deltaStr != null && (
        <div className="kpi-delta" style={{whiteSpace:"nowrap", overflow:"hidden", textOverflow:"ellipsis"}}>
          <span className={delta >= 0 ? "up" : "down"}>{delta >= 0 ? "▲" : "▼"} {deltaStr}</span>
          <span style={{opacity:0.7, fontSize:11}}>{deltaLabel}</span>
        </div>
      )}
      {sparkValues && <Sparkline values={sparkValues} color={color}/>}
    </div>
  );
}

function Dashboard({ periodId, setPeriodId, filter, setFilter }) {
  const period = useMemo(() => resolvePeriod(periodId), [periodId]);
  const txnsAll = useMemo(() => txnsInPeriod(period), [period]);
  const txnsFiltered = useMemo(() => txnsWithFilter(period, filter), [period, filter]);
  const summary = useMemo(() => summarize(txnsAll), [txnsAll]);

  // Compare to a comparable prior window:
  //  - "this-month" → first N days of last month (N = days elapsed)
  //  - "last-month" → month before that
  //  - "ytd"        → same Jan→today range last year
  //  - else         → equal-length window immediately before
  const prevPeriod = useMemo(() => {
    const now = TODAY;
    if (periodId === "this-month") {
      const s = startOfMonth(addMonths(now, -1));
      const e = new Date(s.getFullYear(), s.getMonth(), now.getDate(), 23, 59, 59);
      return { start: s, end: e, label: "prev" };
    }
    if (periodId === "last-month") {
      const s = startOfMonth(addMonths(now, -2));
      return { start: s, end: endOfMonth(s), label: "prev" };
    }
    if (periodId === "ytd") {
      return { start: new Date(now.getFullYear() - 1, 0, 1), end: new Date(now.getFullYear() - 1, now.getMonth(), now.getDate(), 23, 59, 59), label: "prev" };
    }
    const span = period.end - period.start;
    return { start: new Date(period.start.getTime() - span - 1), end: new Date(period.start.getTime() - 1), label: "prev" };
  }, [period, periodId]);
  const prevSummary = useMemo(() => summarize(txnsInPeriod(prevPeriod)), [prevPeriod]);

  const pctDelta = (cur, prev) => prev === 0 ? 0 : ((cur - prev) / prev) * 100;

  // Sparklines: 30-day rolling totals for income/expense/net
  const spark = useMemo(() => {
    const days = Math.min(30, Math.max(7, daysBetween(period.start, period.end) + 1));
    const buckets = Array.from({ length: days }, () => ({ income: 0, expense: 0 }));
    for (const t of txnsAll) {
      const di = Math.floor((t.date - period.start) / 86400000);
      if (di < 0 || di >= days) continue;
      if (t.flow === "income") buckets[di].income += t.amount;
      else if (t.flow === "expense") buckets[di].expense += t.amount;
    }
    return {
      income: buckets.map(b => b.income),
      expense: buckets.map(b => b.expense),
      net: buckets.map(b => b.income - b.expense),
    };
  }, [txnsAll, period]);

  return (
    <div className="page">
      <div style={{display:"flex", alignItems:"flex-end", justifyContent:"space-between", gap:16, flexWrap:"wrap"}}>
        <div>
          <h1 className="h1">Dashboard</h1>
          <div className="subtitle">{period.label} · {txnsAll.length} transactions across {ACCOUNTS.filter(a => a.kind !== "real_estate").length} accounts</div>
        </div>
      </div>

      <PeriodControl value={periodId} onChange={setPeriodId}/>

      <FilterBanner filter={filter} onClear={() => setFilter(null)}/>

      <div className="kpi-grid">
        <KpiCard label="Net Cash Flow"
                 value={fmtCurrency(summary.net)}
                 valueClass={summary.net >= 0 ? "pos" : "neg"}
                 delta={pctDelta(summary.net, prevSummary.net)}
                 deltaLabel="vs prev period"
                 sparkValues={spark.net} color={summary.net >= 0 ? "#4ade80" : "var(--destructive)"}/>
        <KpiCard label="Income"
                 value={fmtCurrency(summary.income)}
                 delta={pctDelta(summary.income, prevSummary.income)}
                 deltaLabel="vs prev period"
                 sparkValues={spark.income} color="#22c55e"/>
        <KpiCard label="Expenses"
                 value={fmtCurrency(summary.expense)}
                 delta={pctDelta(summary.expense, prevSummary.expense)}
                 deltaLabel="vs prev period"
                 sparkValues={spark.expense} color="#f43f5e"/>
        <KpiCard label="Savings Rate"
                 value={(summary.income > 0 ? (summary.net / summary.income) * 100 : 0).toFixed(1) + "%"}
                 sub={<span style={{color:"var(--muted-foreground)"}}><Icon name="Target" size={12}/> Goal 35%</span>}
                 sparkValues={spark.net.map((n, i) => spark.income[i] > 0 ? (n / spark.income[i]) * 100 : 0)} color="#a78bfa"/>
      </div>

      {/* Sankey */}
      <div className="card">
        <div className="section-head">
          <div>
            <div className="h">Cash Flow</div>
            <div className="sub">Where every dollar earned in {period.label} ended up. Click any node to filter the dashboard.</div>
          </div>
          <div className="filter-row">
            <span className="label">Total Income</span>
            <span className="num" style={{fontWeight:600}}>{fmtCurrency(summary.income)}</span>
            <span style={{margin:"0 6px",opacity:0.5}}>·</span>
            <span className="label">Net</span>
            <span className="num" style={{fontWeight:600, color: summary.net >= 0 ? "#4ade80" : "var(--destructive)"}}>{fmtCurrency(summary.net)}</span>
          </div>
        </div>
        <Sankey txns={txnsAll} filter={filter} onFilter={setFilter}/>
      </div>

      {/* Transactions + Top spending side-by-side */}
      <div style={{display:"grid", gridTemplateColumns:"2fr 1fr", gap:16}}>
        <div className="card">
          <div className="section-head">
            <div>
              <div className="h">Recent Transactions</div>
              <div className="sub">{txnsFiltered.length} of {txnsAll.length} in {period.label}{filter ? " · filtered" : ""}</div>
            </div>
            <button className="btn btn--ghost btn--sm">View all <Icon name="ChevronRight" size={12}/></button>
          </div>
          <TransactionsTable txns={txnsFiltered.slice(0, 12)} compact onClickCategory={(t) => setFilter({ catId: t.catId })}/>
        </div>
        <TopSpendCard txns={txnsAll} onPick={(catId) => setFilter({ catId })}/>
      </div>
    </div>
  );
}

function TopSpendCard({ txns, onPick }) {
  const cats = useMemo(() => {
    const m = groupByCat(txns);
    const expense = [...m.values()].filter(c => c.flow === "expense").sort((a, b) => b.total - a.total);
    return expense.slice(0, 6);
  }, [txns]);
  const totalExp = cats.reduce((s, c) => s + c.total, 0);

  return (
    <div className="card">
      <div className="section-head">
        <div>
          <div className="h">Top Spending</div>
          <div className="sub">By category</div>
        </div>
        <Icon name="PieChart" size={14} color="var(--muted-foreground)"/>
      </div>
      <div style={{padding: "8px 0"}}>
        {cats.map(c => {
          const meta = CATEGORIES.expense[c.catId];
          const pct = totalExp ? (c.total / totalExp) * 100 : 0;
          return (
            <div key={c.catId}
                 onClick={() => onPick(c.catId)}
                 style={{padding:"10px 20px", cursor:"pointer"}}
                 className="topspend-row"
                 onMouseEnter={e => e.currentTarget.style.background = "var(--secondary)"}
                 onMouseLeave={e => e.currentTarget.style.background = ""}>
              <div style={{display:"flex", justifyContent:"space-between", alignItems:"baseline", marginBottom:6}}>
                <div style={{display:"flex", alignItems:"center", gap:8}}>
                  <span className="swatch" style={{background: meta.color}}/>
                  <span style={{fontSize:13.5, fontWeight:500}}>{meta.name}</span>
                </div>
                <div className="mono" style={{fontSize:13}}>{fmtCurrency(c.total)}</div>
              </div>
              <div style={{position:"relative", height:5, background:"var(--secondary)", borderRadius:3, overflow:"hidden"}}>
                <div style={{position:"absolute", inset:0, width: pct + "%", background: meta.color, borderRadius:3}}/>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

window.Dashboard = Dashboard;
