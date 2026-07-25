// Mock data for personal finance app — high earner, salary + 7 rentals.
// Everything denormalized as flat transactions; views aggregate.

(function () {
  const fmtCurrency = (n, opts = {}) => {
    const { sign = false, decimals = 0, compact = false } = opts;
    const abs = Math.abs(n);
    let str;
    if (compact && abs >= 1000) {
      if (abs >= 1_000_000) str = (abs / 1_000_000).toFixed(abs >= 10_000_000 ? 1 : 2) + "M";
      else str = (abs / 1000).toFixed(abs >= 100_000 ? 0 : 1) + "k";
      str = "$" + str;
    } else {
      str = "$" + abs.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
    }
    if (n < 0) return "−" + str;
    return sign ? "+" + str : str;
  };
  const fmtPct = (n, decimals = 1) => (n >= 0 ? "+" : "") + n.toFixed(decimals) + "%";

  // ───── Category tree ─────
  // 'income' or 'expense'. Each category has subs; transactions reference sub IDs.
  const CATEGORIES = {
    income: {
      salary: { name: "Salary", color: "#38bdf8", subs: {
        base_salary: { name: "Base Salary", color: "#38bdf8" },
        bonus: { name: "Bonus & RSU", color: "#7dd3fc" },
      }},
      rentals: { name: "Rental Income", color: "#22c55e", subs: {
        prop_aspen: { name: "1247 Aspen Ct", color: "#22c55e" },
        prop_birch: { name: "88 Birch Ave",  color: "#16a34a" },
        prop_cedar: { name: "412 Cedar Ln",  color: "#15803d" },
        prop_dover: { name: "9 Dover Rd",    color: "#4ade80" },
        prop_elm:   { name: "2210 Elm St",   color: "#86efac" },
        prop_fern:  { name: "55 Fern Way",   color: "#34d399" },
        prop_grove: { name: "1771 Grove Pl", color: "#10b981" },
      }},
      investments: { name: "Investments", color: "#a78bfa", subs: {
        dividends: { name: "Dividends", color: "#a78bfa" },
        interest: { name: "Interest", color: "#c4b5fd" },
      }},
    },
    expense: {
      housing: { name: "Housing", color: "#f43f5e", subs: {
        mortgage_primary: { name: "Mortgage — Primary", color: "#f43f5e" },
        rental_mortgages: { name: "Rental Mortgages", color: "#fb7185" },
        property_tax: { name: "Property Tax", color: "#fda4af" },
        utilities: { name: "Utilities", color: "#fecdd3" },
        hoa: { name: "HOA & Maintenance", color: "#e11d48" },
      }},
      food: { name: "Food", color: "#f97316", subs: {
        groceries: { name: "Groceries", color: "#f97316" },
        restaurants: { name: "Restaurants", color: "#fb923c" },
        coffee: { name: "Coffee & Cafés", color: "#fdba74" },
      }},
      transport: { name: "Transport", color: "#eab308", subs: {
        fuel: { name: "Fuel", color: "#eab308" },
        rideshare: { name: "Rideshare", color: "#facc15" },
        auto_insurance: { name: "Auto Insurance", color: "#fde047" },
      }},
      leisure: { name: "Leisure", color: "#06b6d4", subs: {
        travel: { name: "Travel", color: "#06b6d4" },
        entertainment: { name: "Entertainment", color: "#22d3ee" },
        subscriptions: { name: "Subscriptions", color: "#67e8f9" },
        shopping: { name: "Shopping", color: "#0891b2" },
      }},
      health: { name: "Health", color: "#ec4899", subs: {
        insurance_health: { name: "Health Insurance", color: "#ec4899" },
        gym: { name: "Gym & Fitness", color: "#f472b6" },
        medical: { name: "Medical & Pharmacy", color: "#f9a8d4" },
      }},
      taxes: { name: "Taxes", color: "#94a3b8", subs: {
        federal: { name: "Federal Tax", color: "#94a3b8" },
        state: { name: "State Tax", color: "#cbd5e1" },
      }},
      savings: { name: "Savings & Invest", color: "#10b981", subs: {
        retirement_401k: { name: "401(k) Contribution", color: "#10b981" },
        brokerage_dca: { name: "Brokerage DCA", color: "#34d399" },
        emergency_fund: { name: "Emergency Fund", color: "#6ee7b7" },
      }},
    },
  };

  // helpers
  function categoryLookup(subId) {
    for (const flow of ["income", "expense"]) {
      for (const [catId, cat] of Object.entries(CATEGORIES[flow])) {
        if (cat.subs[subId]) return { flow, catId, catName: cat.name, catColor: cat.color, subId, subName: cat.subs[subId].name, subColor: cat.subs[subId].color };
      }
    }
    return null;
  }

  // ───── Accounts ─────
  const ACCOUNTS = [
    { id: "chk_chase", name: "Chase Checking", kind: "checking", balance: 38420 },
    { id: "sav_ally",  name: "Ally HYSA",       kind: "savings",  balance: 142800 },
    { id: "brk_fidel", name: "Fidelity Brokerage", kind: "brokerage", balance: 612400 },
    { id: "ret_401k",  name: "Vanguard 401(k)",     kind: "retirement", balance: 487200 },
    { id: "roth_ira",  name: "Roth IRA",            kind: "retirement", balance: 89600 },
    { id: "hsa_lively",name: "HSA — Lively",        kind: "retirement", balance: 32100 },
    { id: "cc_amex",   name: "Amex Platinum",       kind: "credit",   balance: -8240 },
    { id: "cc_chase",  name: "Chase Sapphire",      kind: "credit",   balance: -3120 },
    { id: "crypto",    name: "Coinbase",            kind: "brokerage", balance: 41600 },
    { id: "prop_primary", name: "Primary Residence", kind: "real_estate", balance: 1240000, debt: 612000 },
    { id: "prop_aspen", name: "1247 Aspen Ct", kind: "real_estate", balance: 580000, debt: 340000 },
    { id: "prop_birch", name: "88 Birch Ave", kind: "real_estate", balance: 425000, debt: 218000 },
    { id: "prop_cedar", name: "412 Cedar Ln", kind: "real_estate", balance: 510000, debt: 295000 },
    { id: "prop_dover", name: "9 Dover Rd", kind: "real_estate", balance: 380000, debt: 195000 },
    { id: "prop_elm", name: "2210 Elm St", kind: "real_estate", balance: 460000, debt: 240000 },
    { id: "prop_fern", name: "55 Fern Way", kind: "real_estate", balance: 395000, debt: 188000 },
    { id: "prop_grove", name: "1771 Grove Pl", kind: "real_estate", balance: 525000, debt: 305000 },
  ];

  // ───── Synthesize transactions for ~14 months ─────
  // Seeded RNG for stability
  function mulberry32(seed) { return function () { let t = (seed += 0x6D2B79F5); t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; }
  const rng = mulberry32(42);
  const rand = (min, max) => min + rng() * (max - min);
  const pick = (arr) => arr[Math.floor(rng() * arr.length)];

  const TODAY = new Date(2026, 4, 16); // May 16 2026
  const TXNS = [];
  let _id = 1;
  function addTxn(d, account, subId, amount, merchant, note) {
    const cat = categoryLookup(subId);
    TXNS.push({
      id: "t" + String(_id++).padStart(5, "0"),
      date: new Date(d),
      account,
      subId,
      catId: cat?.catId,
      flow: cat?.flow,
      amount: Math.round(amount * 100) / 100,
      merchant,
      note: note || "",
    });
  }

  const RENT = { prop_aspen: 4200, prop_birch: 3100, prop_cedar: 3700, prop_dover: 2800, prop_elm: 3400, prop_fern: 2900, prop_grove: 3950 };
  const RENT_MORTGAGE = { prop_aspen: 1850, prop_birch: 1340, prop_cedar: 1620, prop_dover: 1180, prop_elm: 1450, prop_fern: 1240, prop_grove: 1720 };

  // 14 months back so YTD + last 12 mo + comparisons all have data
  for (let mAgo = 13; mAgo >= 0; mAgo--) {
    const monthStart = new Date(TODAY.getFullYear(), TODAY.getMonth() - mAgo, 1);
    const Y = monthStart.getFullYear();
    const M = monthStart.getMonth();
    const lastDay = new Date(Y, M + 1, 0).getDate();
    const isCurrent = mAgo === 0;
    const maxDay = isCurrent ? TODAY.getDate() : lastDay;

    // Salary biweekly: 1st & 15th
    [1, 15].forEach(day => {
      if (day > maxDay) return;
      addTxn(new Date(Y, M, day), "chk_chase", "base_salary", 11250 + rand(-200, 200), "Vortex Labs Payroll", "Biweekly direct deposit");
    });
    // Bonus quarterly
    if ([2, 5, 8, 11].includes(M) && 5 <= maxDay) {
      addTxn(new Date(Y, M, 5), "chk_chase", "bonus", 18000 + rand(-2000, 4000), "Vortex Labs RSU", "Quarterly RSU vest");
    }
    // Rental income on 3rd
    if (3 <= maxDay) Object.entries(RENT).forEach(([prop, amt]) => addTxn(new Date(Y, M, 3), "chk_chase", prop, amt + rand(-50, 50), pick(["Zelle from tenant", "ACH deposit", "Cozy.co payout"]), prop.replace("prop_", "").toUpperCase()));
    // Dividends + interest
    if (28 <= maxDay) {
      addTxn(new Date(Y, M, 28), "brk_fidel", "dividends", 480 + rand(-80, 200), "Fidelity Brokerage", "Quarterly distribution");
      addTxn(new Date(Y, M, 28), "sav_ally", "interest", 540 + rand(-30, 80), "Ally Savings", "HYSA interest");
    }

    // ── Expenses ──
    if (1 <= maxDay) addTxn(new Date(Y, M, 1), "chk_chase", "mortgage_primary", 4280, "Wells Fargo Mortgage", "Primary residence");
    if (2 <= maxDay) Object.entries(RENT_MORTGAGE).forEach(([prop, amt]) => {
      const subId = "rental_mortgages";
      addTxn(new Date(Y, M, 2), "chk_chase", subId, amt, "Rental Mortgage — " + prop.replace("prop_", "").toUpperCase(), "");
    });
    if (10 <= maxDay) addTxn(new Date(Y, M, 10), "chk_chase", "property_tax", 1850 + rand(-100, 100), "County Assessor", "Quarterly property tax accrual");
    // Utilities clusters
    for (let i = 0; i < 5 && rng() > 0.05; i++) { const d = Math.min(maxDay, 6 + Math.floor(rng() * 18)); addTxn(new Date(Y, M, d), "cc_chase", "utilities", 120 + rand(0, 280), pick(["PG&E", "Comcast Xfinity", "Verizon", "Water Dept", "ConEd"]), ""); }
    // Groceries: weekly
    for (let i = 0; i < 5; i++) { const d = Math.min(maxDay, 3 + i * 6 + Math.floor(rng() * 3)); if (d > maxDay) break; addTxn(new Date(Y, M, d), "cc_amex", "groceries", 180 + rand(20, 260), pick(["Whole Foods", "Trader Joe's", "Costco", "Erewhon"]), ""); }
    // Restaurants
    const nDining = 6 + Math.floor(rng() * 8);
    for (let i = 0; i < nDining; i++) { const d = Math.min(maxDay, 1 + Math.floor(rng() * 28)); if (d > maxDay) continue; addTxn(new Date(Y, M, d), "cc_amex", "restaurants", 60 + rand(20, 220), pick(["Tartine", "Nopa", "Kin Khao", "Lazy Bear", "Sushi Yoshi", "Octavia", "Outerlands", "Mister Jiu's"]), ""); }
    // Coffee
    for (let i = 0; i < 10; i++) { const d = Math.min(maxDay, 1 + Math.floor(rng() * 28)); if (d > maxDay) continue; addTxn(new Date(Y, M, d), "cc_chase", "coffee", 5 + rand(2, 12), pick(["Sightglass", "Blue Bottle", "Ritual", "Equator"]), ""); }
    // Fuel + rideshare
    for (let i = 0; i < 3; i++) { const d = Math.min(maxDay, 4 + i * 9); if (d > maxDay) continue; addTxn(new Date(Y, M, d), "cc_chase", "fuel", 60 + rand(10, 40), pick(["Shell", "Chevron", "Costco Gas"]), ""); }
    for (let i = 0; i < 5; i++) { const d = Math.min(maxDay, 2 + Math.floor(rng() * 26)); if (d > maxDay) continue; addTxn(new Date(Y, M, d), "cc_amex", "rideshare", 18 + rand(5, 35), pick(["Uber", "Lyft", "Waymo"]), ""); }
    if (15 <= maxDay) addTxn(new Date(Y, M, 15), "chk_chase", "auto_insurance", 280, "Geico", "");
    // Travel — occasional big spikes
    if (rng() < 0.55) { const d = Math.min(maxDay, 5 + Math.floor(rng() * 22)); if (d <= maxDay) addTxn(new Date(Y, M, d), "cc_amex", "travel", 800 + rand(200, 4200), pick(["United Airlines", "Delta", "Marriott Bonvoy", "Airbnb", "Hyatt", "American Airlines"]), ""); }
    // Entertainment
    for (let i = 0; i < 3; i++) { const d = Math.min(maxDay, 2 + Math.floor(rng() * 27)); if (d > maxDay) continue; addTxn(new Date(Y, M, d), "cc_chase", "entertainment", 40 + rand(10, 120), pick(["Alamo Drafthouse", "AMC", "Live Nation", "SF Symphony"]), ""); }
    // Subscriptions monthly
    [["Netflix", 18], ["Spotify", 12], ["NYT", 25], ["Apple One", 38], ["iCloud+", 10], ["Substack", 8], ["Github", 21]].forEach(([m, v], i) => { const d = Math.min(maxDay, 4 + i * 3); if (d <= maxDay) addTxn(new Date(Y, M, d), "cc_amex", "subscriptions", v, m, ""); });
    // Shopping
    for (let i = 0; i < 4; i++) { const d = Math.min(maxDay, 1 + Math.floor(rng() * 28)); if (d > maxDay) continue; addTxn(new Date(Y, M, d), "cc_amex", "shopping", 80 + rand(20, 380), pick(["Amazon", "Apple", "Lululemon", "Patagonia", "Best Buy", "Sephora"]), ""); }
    // Health
    if (5 <= maxDay) addTxn(new Date(Y, M, 5), "chk_chase", "insurance_health", 640, "Aetna", "Premium");
    if (1 <= maxDay) addTxn(new Date(Y, M, 1), "cc_chase", "gym", 220, "Equinox", "Monthly membership");
    if (rng() < 0.6) { const d = Math.min(maxDay, 5 + Math.floor(rng() * 22)); if (d <= maxDay) addTxn(new Date(Y, M, d), "cc_chase", "medical", 60 + rand(20, 320), pick(["Walgreens Rx", "Stanford Medical", "One Medical", "CVS"]), ""); }
    // Taxes monthly
    if (1 <= maxDay) addTxn(new Date(Y, M, 1), "chk_chase", "federal", 6800 + rand(-200, 200), "IRS Withholding", "");
    if (1 <= maxDay) addTxn(new Date(Y, M, 1), "chk_chase", "state", 1900 + rand(-80, 80), "CA Franchise Tax", "");
    // HOA / maint occasional
    if (rng() < 0.55) { const d = Math.min(maxDay, 12 + Math.floor(rng() * 12)); if (d <= maxDay) addTxn(new Date(Y, M, d), "cc_chase", "hoa", 180 + rand(40, 380), pick(["HOA Fees", "Roof Repair", "Plumber", "Cleaning Service", "Landscaping"]), ""); }
    // Savings flows
    if (15 <= maxDay) addTxn(new Date(Y, M, 15), "chk_chase", "retirement_401k", 2350, "Vanguard 401(k)", "Auto-contribution");
    if (15 <= maxDay) addTxn(new Date(Y, M, 15), "chk_chase", "brokerage_dca", 4000, "Fidelity DCA", "VOO / VTI DCA");
    if (1 <= maxDay) addTxn(new Date(Y, M, 1), "chk_chase", "emergency_fund", 1500, "Ally HYSA Transfer", "");
  }

  // ───── Net Worth history ─────
  const NW_HISTORY = (() => {
    const arr = [];
    const start = 2_900_000;
    for (let mAgo = 23; mAgo >= 0; mAgo--) {
      const d = new Date(TODAY.getFullYear(), TODAY.getMonth() - mAgo, 1);
      const t = (23 - mAgo) / 23;
      const drift = start + t * 750_000;
      const vol = Math.sin((23 - mAgo) * 0.7) * 28_000 + (rng() - 0.5) * 22_000;
      arr.push({ date: d, value: Math.round(drift + vol) });
    }
    arr.push({ date: TODAY, value: 3_686_400 });
    return arr;
  })();

  // ───── Budgets ─────
  const BUDGETS = [
    { id: "b1", subId: "groceries",       amount: 1400, period: "month", label: "Groceries" },
    { id: "b2", subId: "restaurants",     amount: 1100, period: "month", label: "Restaurants" },
    { id: "b3", subId: "coffee",          amount: 180,  period: "month", label: "Coffee" },
    { id: "b4", subId: "travel",          amount: 2800, period: "month", label: "Travel" },
    { id: "b5", subId: "shopping",        amount: 900,  period: "month", label: "Shopping" },
    { id: "b6", subId: "rideshare",       amount: 280,  period: "month", label: "Rideshare" },
    { id: "b7", subId: "entertainment",   amount: 320,  period: "month", label: "Entertainment" },
    { id: "b8", subId: "fuel",            amount: 350,  period: "month", label: "Fuel" },
    { id: "b9", subId: "subscriptions",   amount: 160,  period: "month", label: "Subscriptions" },
    { id: "b10", subId: "groceries",      amount: 380,  period: "week",  label: "Groceries (weekly)" },
  ];

  window.AppData = {
    CATEGORIES, ACCOUNTS, TXNS, NW_HISTORY, BUDGETS, TODAY,
    categoryLookup, fmtCurrency, fmtPct,
  };
})();
