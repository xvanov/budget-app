// Transactions: full table + recategorize popover.
// Reused on Dashboard via <TransactionsTable/>.

function TransactionsTable({ txns, compact = false, onClickCategory }) {
  const [picker, setPicker] = useState(null); // { txnId, x, y, flow }
  const [reclassified, setReclassified] = useState({}); // txnId -> newSubId

  function openPicker(e, t) {
    e.stopPropagation();
    const r = e.currentTarget.getBoundingClientRect();
    setPicker({ txnId: t.id, x: r.left, y: r.bottom + 4, flow: t.flow });
  }

  return (
    <>
      <table className="tbl">
        <thead>
          <tr>
            <th style={{width: 80}}>Date</th>
            <th>Merchant</th>
            <th>Category</th>
            {!compact && <th>Account</th>}
            <th className="text-right" style={{width: 110}}>Amount</th>
          </tr>
        </thead>
        <tbody>
          {txns.map(t => {
            const effectiveSub = reclassified[t.id] || t.subId;
            const c = categoryLookup(effectiveSub);
            const acc = ACCOUNTS.find(a => a.id === t.account);
            return (
              <tr key={t.id}>
                <td className="mono" style={{color:"var(--muted-foreground)", fontSize:12}}>{fmtDate(t.date)}</td>
                <td>
                  <div className="cell-merchant">
                    <span style={{fontWeight: 500}}>{t.merchant}</span>
                    {t.note && <span style={{fontSize:11, color:"var(--muted-foreground)"}}>· {t.note}</span>}
                    {reclassified[t.id] && <span className="chip" style={{height:18, fontSize:10, padding:"0 6px"}}><Icon name="Sparkles" size={9}/> reclassified</span>}
                  </div>
                </td>
                <td>
                  <span className="cell-cat" onClick={(e) => openPicker(e, t)} title="Click to re-categorize">
                    <span className="dot" style={{background: c.subColor}}/>
                    <span style={{fontSize:12, color:"var(--muted-foreground)"}}>{c.catName} ›</span>
                    <span>{c.subName}</span>
                  </span>
                </td>
                {!compact && (
                  <td style={{fontSize:12.5, color:"var(--muted-foreground)"}}>{acc?.name}</td>
                )}
                <td className={"text-right mono " + (t.flow === "income" ? "amt-pos" : "amt-neg")}>
                  {t.flow === "income" ? "+" : "−"}{fmtCurrency(t.amount, { decimals: 2 }).replace("$","$")}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {picker && (
        <CategoryPicker
          anchor={{ x: picker.x, y: picker.y }}
          flow={picker.flow}
          onClose={() => setPicker(null)}
          onPick={(subId) => { setReclassified(r => ({ ...r, [picker.txnId]: subId })); setPicker(null); }}
        />
      )}
    </>
  );
}

function Transactions({ periodId, setPeriodId, filter, setFilter }) {
  const period = useMemo(() => resolvePeriod(periodId), [periodId]);
  const [search, setSearch] = useState("");
  const [accountFilter, setAccountFilter] = useState(null);

  const txns = useMemo(() => {
    let arr = txnsWithFilter(period, filter);
    if (accountFilter) arr = arr.filter(t => t.account === accountFilter);
    if (search.trim()) {
      const q = search.trim().toLowerCase();
      arr = arr.filter(t => (t.merchant + " " + t.note).toLowerCase().includes(q));
    }
    return arr.sort((a, b) => b.date - a.date);
  }, [period, filter, search, accountFilter]);

  const summary = summarize(txns);
  const bankAccts = ACCOUNTS.filter(a => a.kind !== "real_estate" && a.kind !== "retirement");

  return (
    <div className="page">
      <div>
        <h1 className="h1">Transactions</h1>
        <div className="subtitle">{txns.length} transactions · {fmtCurrency(summary.income)} in · {fmtCurrency(summary.expense)} out</div>
      </div>

      <PeriodControl value={periodId} onChange={setPeriodId}/>

      <div className="filter-row">
        <span className="label">Account</span>
        <span className={`chip ${!accountFilter ? "is-active" : ""}`} onClick={() => setAccountFilter(null)}>All</span>
        {bankAccts.map(a => (
          <span key={a.id} className={`chip ${accountFilter === a.id ? "is-active" : ""}`}
                onClick={() => setAccountFilter(a.id === accountFilter ? null : a.id)}>
            {a.name}
          </span>
        ))}
        <div style={{flex:1}}/>
        <div style={{display:"flex", alignItems:"center", gap:6, border:"1px solid var(--input)", borderRadius:6, padding:"0 10px", height:32, background:"var(--background)"}}>
          <Icon name="Search" size={14} color="var(--muted-foreground)"/>
          <input value={search} onChange={e => setSearch(e.target.value)}
                 placeholder="Search merchants…"
                 style={{border:0, outline:0, background:"transparent", color:"var(--foreground)", width:200, fontSize:13}}/>
        </div>
      </div>

      <FilterBanner filter={filter} onClear={() => setFilter(null)}/>

      <div className="card">
        <TransactionsTable txns={txns} onClickCategory={(t) => setFilter({ catId: t.catId })}/>
      </div>
    </div>
  );
}

window.TransactionsTable = TransactionsTable;
window.Transactions = Transactions;
