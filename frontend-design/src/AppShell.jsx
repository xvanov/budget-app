// App shell — sidebar nav, top bar, route switching, shared filter state.

function AppShell() {
  const [route, setRoute] = useState(() => localStorage.getItem("budget-route") || "/");
  const [periodId, setPeriodId] = useState(() => localStorage.getItem("budget-period") || "this-month");
  const [filter, setFilter] = useState(null);

  useEffect(() => localStorage.setItem("budget-route", route), [route]);
  useEffect(() => localStorage.setItem("budget-period", periodId), [periodId]);

  // Compute net worth for sidebar footer
  const totalNW = useMemo(() => {
    let nw = 0;
    for (const a of ACCOUNTS) {
      if (a.kind === "real_estate") nw += (a.balance - (a.debt || 0));
      else nw += a.balance;
    }
    return nw;
  }, []);

  const navItems = [
    { id: "/",         label: "Dashboard",    icon: "LayoutDashboard" },
    { id: "/txns",     label: "Transactions", icon: "Receipt" },
    { id: "/networth", label: "Net Worth",    icon: "Wallet" },
    { id: "/compare",  label: "Compare",      icon: "GitMerge" },
    { id: "/trends",   label: "Trends",       icon: "TrendingUp" },
    { id: "/budgets",  label: "Budgets",      icon: "Target" },
  ];

  let pageTitle = "";
  let pageContent;
  switch (route) {
    case "/":
      pageTitle = "Dashboard";
      pageContent = <Dashboard periodId={periodId} setPeriodId={setPeriodId} filter={filter} setFilter={setFilter}/>;
      break;
    case "/txns":
      pageTitle = "Transactions";
      pageContent = <Transactions periodId={periodId} setPeriodId={setPeriodId} filter={filter} setFilter={setFilter}/>;
      break;
    case "/networth":
      pageTitle = "Net Worth";
      pageContent = <NetWorth/>;
      break;
    case "/compare":
      pageTitle = "Compare";
      pageContent = <Compare/>;
      break;
    case "/trends":
      pageTitle = "Trends";
      pageContent = <Trends filter={filter} setFilter={setFilter}/>;
      break;
    case "/budgets":
      pageTitle = "Budgets";
      pageContent = <Budgets/>;
      break;
    default:
      pageTitle = "Dashboard";
      pageContent = <Dashboard periodId={periodId} setPeriodId={setPeriodId} filter={filter} setFilter={setFilter}/>;
  }

  return (
    <div className="app dark">
      <aside className="sb">
        <div className="sb-brand">
          <img src="src/assets/logo.png" className="sb-logo" alt=""/>
          <div className="sb-brandwrap">
            <span className="sb-brandname">Tally</span>
            <span className="sb-brandtag">Personal Finance</span>
          </div>
        </div>
        <div className="sb-strip">NET WORTH · {fmtCurrency(totalNW, {compact: true})}</div>

        <div className="sb-section">
          <div className="sb-section-label">Overview</div>
          {navItems.map(item => (
            <button key={item.id}
              className={`sb-item ${route === item.id ? "is-active" : ""}`}
              onClick={() => { setRoute(item.id); }}>
              <Icon name={item.icon} size={16}/> {item.label}
            </button>
          ))}
        </div>

        <div className="sb-section">
          <div className="sb-section-label">Accounts</div>
          <button className="sb-item">
            <Icon name="Building" size={16}/> Properties
            <span className="sb-item-meta">7</span>
          </button>
          <button className="sb-item">
            <Icon name="Briefcase" size={16}/> Employment
            <span className="sb-item-meta">1</span>
          </button>
          <button className="sb-item">
            <Icon name="Tag" size={16}/> Categories
          </button>
          <button className="sb-item">
            <Icon name="Settings" size={16}/> Settings
          </button>
        </div>

        <div className="sb-foot">
          <div className="sb-user">
            <div className="avatar">AK</div>
            <div className="sb-user-meta">
              <div className="sb-user-name">Alex Kim</div>
              <div className="sb-user-email">alex@kim.house</div>
            </div>
          </div>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <div className="topbar-title">{pageTitle}</div>
          <div className="topbar-spacer"/>
          <button className="iconbtn" title="Search"><Icon name="Search" size={16}/></button>
          <button className="iconbtn" title="Notifications"><Icon name="Activity" size={16}/></button>
          <button className="iconbtn" title="Settings"><Icon name="Settings" size={16}/></button>
          <div className="avatar" style={{width:30,height:30,fontSize:11}}>AK</div>
        </header>
        <main className="content">{pageContent}</main>
      </div>
    </div>
  );
}

window.AppShell = AppShell;
