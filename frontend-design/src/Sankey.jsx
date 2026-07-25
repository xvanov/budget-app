// Multi-level Sankey for income → categories → subcategories.
// Custom layout — no d3. Renders SVG ribbons, supports click-to-filter.

const { useState: _useS, useMemo: _useM, useRef: _useR } = React;

function buildSankeyModel(txns) {
  // Aggregate
  const incomeBySub = new Map();   // income subId -> total
  const incomeByCat = new Map();   // income catId -> total
  const expenseBySub = new Map();  // expense subId -> total
  const expenseByCat = new Map();  // expense catId -> total

  for (const t of txns) {
    if (t.flow === "income") {
      incomeBySub.set(t.subId, (incomeBySub.get(t.subId) || 0) + t.amount);
      incomeByCat.set(t.catId, (incomeByCat.get(t.catId) || 0) + t.amount);
    } else if (t.flow === "expense") {
      expenseBySub.set(t.subId, (expenseBySub.get(t.subId) || 0) + t.amount);
      expenseByCat.set(t.catId, (expenseByCat.get(t.catId) || 0) + t.amount);
    }
  }

  const totalIncome = [...incomeBySub.values()].reduce((a, b) => a + b, 0);
  const totalExpense = [...expenseBySub.values()].reduce((a, b) => a + b, 0);

  // 4 columns:
  // 0: Income sources (sub level)
  // 1: Income category groups (Salary, Rentals, Investments)
  // 2: Expense category groups (Housing, Food, ...) + "Net Savings" sink
  // 3: Expense subcategories (omitted for Net Savings)
  const nodes = []; const nodeById = new Map();
  function addNode(id, label, value, color, col, kind, ref) {
    const n = { id, label, value, color, col, kind, ref, in: 0, out: 0 };
    nodes.push(n); nodeById.set(id, n); return n;
  }

  // Col 0: income subs
  for (const [subId, v] of incomeBySub) {
    const meta = CATEGORIES.income[categoryLookup(subId).catId].subs[subId];
    addNode("isub:" + subId, meta.name, v, meta.color, 0, "income-sub", { subId });
  }
  // Col 1: income cats
  for (const [catId, v] of incomeByCat) {
    const meta = CATEGORIES.income[catId];
    addNode("icat:" + catId, meta.name, v, meta.color, 1, "income-cat", { catId });
  }
  // Col 2: expense cats + net savings node
  for (const [catId, v] of expenseByCat) {
    const meta = CATEGORIES.expense[catId];
    addNode("ecat:" + catId, meta.name, v, meta.color, 2, "expense-cat", { catId });
  }
  const netVal = totalIncome - totalExpense;
  if (netVal > 0) {
    addNode("net:savings", "Net Cash Flow", netVal, "#a3e635", 2, "net", { net: true });
  }
  // Col 3: expense subs
  for (const [subId, v] of expenseBySub) {
    const meta = CATEGORIES.expense[categoryLookup(subId).catId].subs[subId];
    addNode("esub:" + subId, meta.name, v, meta.color, 3, "expense-sub", { subId });
  }

  // Links
  const links = [];
  // Col 0 → Col 1
  for (const [subId, v] of incomeBySub) {
    const catId = categoryLookup(subId).catId;
    links.push({ source: "isub:" + subId, target: "icat:" + catId, value: v });
  }
  // Col 1 → Col 2 (proportional)
  for (const [icatId, ival] of incomeByCat) {
    // Each income cat → each expense cat (+ net) proportional to its share of total income
    const share = ival / totalIncome;
    for (const [ecatId, eval_] of expenseByCat) {
      links.push({ source: "icat:" + icatId, target: "ecat:" + ecatId, value: eval_ * share });
    }
    if (netVal > 0) links.push({ source: "icat:" + icatId, target: "net:savings", value: netVal * share });
  }
  // Col 2 → Col 3 (each expense cat → its subs)
  for (const [catId, v] of expenseByCat) {
    const subs = expenseBySub;
    for (const [subId, sv] of subs) {
      if (categoryLookup(subId).catId === catId) {
        links.push({ source: "ecat:" + catId, target: "esub:" + subId, value: sv });
      }
    }
  }

  return { nodes, nodeById, links, totalIncome, totalExpense, netVal };
}

function layoutSankey(model, width, height, padding = { top: 24, right: 24, bottom: 16, left: 24 }) {
  const { nodes, links } = model;
  const cols = [[], [], [], []];
  for (const n of nodes) cols[n.col].push(n);

  const innerW = width - padding.left - padding.right;
  const innerH = height - padding.top - padding.bottom;
  const nodeW = 14;
  const colX = [0, innerW / 3, (innerW / 3) * 2, innerW].map(x => x + padding.left);

  // Scale factor: each column's total nodes value should equal total (same flow), so scale once.
  const colTotals = cols.map(arr => arr.reduce((s, n) => s + n.value, 0));
  const totalMax = Math.max(...colTotals);
  // gaps
  const nodeGapPx = 8;
  // value→pixel scale; reserve gaps per column
  function colScale(col) {
    const nNodes = cols[col].length;
    const totalGap = Math.max(0, (nNodes - 1)) * nodeGapPx;
    return (innerH - totalGap) / totalMax;
  }

  // Sort each col by value desc for stable layout; col 2 keep "net" at end
  cols.forEach((arr, ci) => {
    arr.sort((a, b) => {
      if (a.kind === "net") return 1;
      if (b.kind === "net") return -1;
      return b.value - a.value;
    });
  });

  // Position nodes
  cols.forEach((arr, ci) => {
    const s = colScale(ci);
    let y = padding.top;
    for (const n of arr) {
      n.x = colX[ci] - (ci === 3 ? nodeW : 0); // last col anchor right
      if (ci === 0) n.x = padding.left;
      n.y0 = y;
      n.h = Math.max(2, n.value * s);
      n.y1 = y + n.h;
      n.w = nodeW;
      y = n.y1 + nodeGapPx;
    }
  });

  // Sort links per node by target/source y to make ribbons cross cleanly
  // (approximate: order by partner's value desc)
  function attachLinks() {
    // group links by source/target
    const outBy = new Map(); const inBy = new Map();
    for (const l of links) {
      if (!outBy.has(l.source)) outBy.set(l.source, []);
      outBy.get(l.source).push(l);
      if (!inBy.has(l.target)) inBy.set(l.target, []);
      inBy.get(l.target).push(l);
    }
    // assign sy, ty within each node
    const { nodeById } = model;
    for (const [nid, arr] of outBy) {
      const src = nodeById.get(nid);
      arr.sort((a, b) => (nodeById.get(a.target).y0) - (nodeById.get(b.target).y0));
      const s = colScale(src.col);
      let y = src.y0;
      for (const l of arr) {
        const lh = Math.max(1.2, l.value * s);
        l.sy0 = y; l.sy1 = y + lh; l._lh = lh;
        y += lh;
      }
    }
    for (const [nid, arr] of inBy) {
      const tgt = nodeById.get(nid);
      arr.sort((a, b) => (nodeById.get(a.source).y0) - (nodeById.get(b.source).y0));
      const s = colScale(tgt.col);
      let y = tgt.y0;
      for (const l of arr) {
        const lh = Math.max(1.2, l.value * s);
        l.ty0 = y; l.ty1 = y + lh;
        y += lh;
      }
    }
  }
  attachLinks();
  return { cols, nodeW, padding, innerW, innerH };
}

function linkPath(l, model) {
  const src = model.nodeById.get(l.source);
  const tgt = model.nodeById.get(l.target);
  const x0 = src.x + src.w, x1 = tgt.x;
  const my = (x0 + x1) / 2;
  const sy = (l.sy0 + l.sy1) / 2, ty = (l.ty0 + l.ty1) / 2;
  const half = (l.sy1 - l.sy0) / 2;
  return `M ${x0},${sy - half}
          C ${my},${sy - half} ${my},${ty - half} ${x1},${ty - half}
          L ${x1},${ty + half}
          C ${my},${ty + half} ${my},${sy + half} ${x0},${sy + half} Z`;
}

function Sankey({ txns, filter, onFilter }) {
  const wrapRef = _useR(null);
  const [size, setSize] = _useS({ w: 900, h: 460 });
  const [hover, setHover] = _useS(null);
  const [tooltip, setTooltip] = _useS(null);

  useEffect(() => {
    if (!wrapRef.current) return;
    const ro = new ResizeObserver(es => { for (const e of es) setSize(s => ({ ...s, w: e.contentRect.width })); });
    ro.observe(wrapRef.current);
    return () => ro.disconnect();
  }, []);

  const model = _useM(() => buildSankeyModel(txns), [txns]);
  const lay = _useM(() => layoutSankey(model, size.w, size.h), [model, size.w, size.h]);

  // Determine which links/nodes are "lit" given filter or hover
  const lit = _useM(() => {
    const lit = { nodes: new Set(), links: new Set() };
    let focusNode = null;
    if (hover?.nodeId) focusNode = hover.nodeId;
    else if (filter?.subId) {
      // try income or expense
      const isubId = "isub:" + filter.subId, esubId = "esub:" + filter.subId;
      if (model.nodeById.has(isubId)) focusNode = isubId;
      else if (model.nodeById.has(esubId)) focusNode = esubId;
    } else if (filter?.catId) {
      const ic = "icat:" + filter.catId, ec = "ecat:" + filter.catId;
      if (model.nodeById.has(ic)) focusNode = ic;
      else if (model.nodeById.has(ec)) focusNode = ec;
    }
    if (!focusNode) return null;
    // BFS forward + backward
    const fwd = new Set([focusNode]);
    let frontier = [focusNode];
    while (frontier.length) {
      const nxt = [];
      for (const l of model.links) {
        if (frontier.includes(l.source) && !fwd.has(l.target)) { fwd.add(l.target); nxt.push(l.target); lit.links.add(l); }
        if (frontier.includes(l.target) && !fwd.has(l.source)) { fwd.add(l.source); nxt.push(l.source); lit.links.add(l); }
      }
      frontier = nxt;
    }
    lit.nodes = fwd;
    return lit;
  }, [model, hover, filter]);

  function onNodeClick(node) {
    if (node.kind === "income-sub" || node.kind === "expense-sub") {
      onFilter?.({ subId: node.ref.subId });
    } else if (node.kind === "income-cat" || node.kind === "expense-cat") {
      onFilter?.({ catId: node.ref.catId });
    } else if (node.kind === "net") {
      onFilter?.(null);
    }
  }

  return (
    <div ref={wrapRef} className="sankey-wrap">
      <div className="sankey-cols">
        <span>Sources</span><span>Income → Allocation</span><span>Subcategories</span>
      </div>
      <svg className="sankey" viewBox={`0 0 ${size.w} ${size.h}`} height={size.h}>
        {/* Links first */}
        {model.links.map((l, i) => {
          const src = model.nodeById.get(l.source);
          const tgt = model.nodeById.get(l.target);
          if (!src || !tgt) return null;
          const isLit = lit && lit.links.has(l);
          const dim = lit && !isLit;
          // Color: blend between source and target
          const color = tgt.kind === "net" ? "#a3e635" : (tgt.color || src.color);
          return (
            <path key={i}
              d={linkPath(l, model)}
              className={`sankey-link ${dim ? "is-dim" : ""} ${isLit ? "is-hot" : ""}`}
              fill={color}
              fillOpacity={isLit ? 0.65 : dim ? 0.08 : 0.32}
              onMouseEnter={(e) => setTooltip({ x: e.clientX, y: e.clientY, label: `${src.label} → ${tgt.label}`, value: l.value, sub: "" })}
              onMouseMove={(e) => setTooltip(t => t && { ...t, x: e.clientX, y: e.clientY })}
              onMouseLeave={() => setTooltip(null)}
            />
          );
        })}
        {/* Nodes */}
        {model.nodes.map(n => {
          const dim = lit && !lit.nodes.has(n.id);
          return (
            <g key={n.id} className="sankey-node"
               onMouseEnter={() => setHover({ nodeId: n.id })}
               onMouseLeave={() => setHover(null)}
               onClick={() => onNodeClick(n)}>
              <rect x={n.x} y={n.y0} width={n.w} height={n.h} fill={n.color} opacity={dim ? 0.3 : 1} rx={2}/>
              <text className="sankey-node-label"
                x={n.col === 3 ? n.x - 6 : n.x + n.w + 6}
                y={n.y0 + Math.min(14, n.h / 2 + 4)}
                textAnchor={n.col === 3 ? "end" : "start"}
                opacity={dim ? 0.35 : 1}>
                {n.label}
              </text>
              {n.h > 26 && (
                <text className="sankey-node-value"
                  x={n.col === 3 ? n.x - 6 : n.x + n.w + 6}
                  y={n.y0 + Math.min(28, n.h / 2 + 18)}
                  textAnchor={n.col === 3 ? "end" : "start"}
                  opacity={dim ? 0.35 : 1}>
                  {fmtCurrency(n.value, { compact: true })}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      {tooltip && (
        <div className="tooltip" style={{ left: tooltip.x + 12, top: tooltip.y + 12 }}>
          <div className="t-label">{tooltip.label}</div>
          <div className="t-value">{fmtCurrency(tooltip.value)}</div>
        </div>
      )}
    </div>
  );
}

window.Sankey = Sankey;
