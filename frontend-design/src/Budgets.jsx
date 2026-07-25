// Budgets — list of budgets with progress bars; subtle red overshoot styling.

function Budgets() {
  const [budgets, setBudgets] = useState(BUDGETS);

  // For each budget, compute current spend in its period vs amount.
  const rows = useMemo(() => {
    return budgets.map(b => {
      const now = TODAY;
      let start, end;
      if (b.period === "month") { start = startOfMonth(now); end = endOfMonth(now); }
      else if (b.period === "week") {
        const day = now.getDay(); // Sunday = 0
        start = new Date(now); start.setDate(now.getDate() - day); start.setHours(0,0,0,0);
        end = new Date(start); end.setDate(start.getDate() + 6); end.setHours(23,59,59,0);
      } else {
        start = startOfMonth(now); end = endOfMonth(now);
      }
      const spend = TXNS
        .filter(t => t.subId === b.subId && t.date >= start && t.date <= end)
        .reduce((s, t) => s + t.amount, 0);
      const spanDays = daysBetween(start, end) + 1;
      const elapsedDays = Math.min(spanDays, Math.max(0, daysBetween(start, now) + 1));
      const expectedPct = (elapsedDays / spanDays) * 100;
      const actualPct = (spend / b.amount) * 100;
      const overshoot = Math.max(0, spend - b.amount);
      const cat = categoryLookup(b.subId);
      return { ...b, start, end, spend, expectedPct, actualPct, overshoot, spanDays, elapsedDays, cat };
    });
  }, [budgets]);

  const overCount = rows.filter(r => r.spend > r.amount).length;
  const totalBudgeted = rows.reduce((s, r) => s + r.amount, 0);
  const totalSpent = rows.reduce((s, r) => s + r.spend, 0);

  return (
    <div className="page">
      <div style={{display:"flex", justifyContent:"space-between", alignItems:"flex-end", gap:16, flexWrap:"wrap"}}>
        <div>
          <h1 className="h1">Budgets</h1>
          <div className="subtitle">{budgets.length} active budgets · {overCount} over the line · pacing dot shows expected progress through the period</div>
        </div>
        <button className="btn btn--primary"><Icon name="Plus" size={14}/> New budget</button>
      </div>

      <div className="kpi-grid" style={{gridTemplateColumns:"repeat(3, 1fr)"}}>
        <KpiCard label="Budgeted this period" value={fmtCurrency(totalBudgeted)} sub={<span><Icon name="Target" size={12}/> Across {budgets.length} envelopes</span>}/>
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
          <div className="seg">
            <button className="seg-item is-active">All</button>
            <button className="seg-item">Monthly</button>
            <button className="seg-item">Weekly</button>
          </div>
        </div>
        <div>
          {rows.map(r => <BudgetRow key={r.id} r={r}/>)}
        </div>
      </div>
    </div>
  );
}

function BudgetRow({ r }) {
  const isOver = r.spend > r.amount;
  const isOnPace = r.actualPct <= r.expectedPct + 5; // 5pp grace
  const remaining = r.amount - r.spend;

  // Track represents 0..MAX_PCT% of budget. Budget line sits inside the track.
  const MAX_PCT = 130;
  const budgetMarkPct = (100 / MAX_PCT) * 100;                   // ~77% of track width
  const fillPct = Math.min(MAX_PCT, r.actualPct);
  const fillWidthPct = (fillPct / MAX_PCT) * 100;
  const overshootStart = budgetMarkPct;
  const overshootEnd = isOver ? Math.min(100, fillWidthPct) : budgetMarkPct;
  const pacingPct = Math.min(MAX_PCT, r.expectedPct) / MAX_PCT * 100;
  const fillClass = isOver ? "over" : (isOnPace ? "ok" : "warn");

  return (
    <div className="budget-row">
      <div>
        <div className="budget-name">
          <span className="swatch" style={{background: r.cat.subColor}}/>
          {r.label}
        </div>
        <div className="budget-period">
          {r.cat.catName} · {r.period === "week" ? "Weekly" : "Monthly"} · {fmtDate(r.start)} → {fmtDate(r.end)}
        </div>
      </div>
      <div className="budget-amt">
        <div className="v mono" style={isOver ? {color:"var(--destructive)"} : null}>{fmtCurrency(r.spend, { decimals: 0 })}</div>
        <div className="label">of {fmtCurrency(r.amount, {decimals:0})}</div>
      </div>
      <div className="budget-amt">
        <div className="v mono" style={remaining < 0 ? {color:"var(--destructive)"} : null}>
          {remaining >= 0 ? fmtCurrency(remaining, {decimals:0}) : "−" + fmtCurrency(Math.abs(remaining), {decimals:0})}
        </div>
        <div className="label">{remaining >= 0 ? "remaining" : "over"}</div>
      </div>
      <div>
        <div className="budget-track" style={{height: 10, position:"relative", overflow:"hidden"}}>
          {/* solid fill (capped at the budget mark when over — the hatch handles the overshoot zone) */}
          <div className={`budget-fill ${fillClass}`}
               style={{
                 width: (isOver ? budgetMarkPct : fillWidthPct) + "%",
                 borderRadius: 4,
               }}/>
          {/* overshoot hatch sits between budget line and fill end, all inside the track */}
          {isOver && (
            <div className="budget-overshoot"
                 style={{
                   left: overshootStart + "%",
                   width: (overshootEnd - overshootStart) + "%",
                   borderRadius: 0,
                 }}/>
          )}
          {/* budget-limit tick at 100% mark (inside the track) */}
          <div style={{
            position:"absolute", top:-3, bottom:-3, left: budgetMarkPct + "%",
            width: 2, background: "var(--foreground)", opacity: 0.55, borderRadius: 1,
          }} title="Budget limit"/>
          {/* pacing tick — where you'd be on track */}
          <div className="budget-pacing"
               style={{left: pacingPct + "%", opacity: 0.35, top: -2, bottom: -2}}
               title={`On pace: ${r.expectedPct.toFixed(0)}%`}/>
        </div>
        <div className={`budget-status ${isOver ? "over" : ""}`} style={{marginTop:6, fontSize:11}}>
          {isOver
            ? <>Over by <span className="mono">{fmtCurrency(r.overshoot, {decimals:0})}</span> · {r.elapsedDays} of {r.spanDays} days in</>
            : <>{r.actualPct.toFixed(0)}% used · {isOnPace ? "on pace" : "ahead of pace"} · {r.elapsedDays} of {r.spanDays} days</>
          }
        </div>
      </div>
    </div>
  );
}

window.Budgets = Budgets;
