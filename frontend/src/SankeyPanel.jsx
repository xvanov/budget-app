import React, { useState, useMemo, useRef, useEffect } from 'react'

function fmtCurrency(cents, opts = {}) {
  if (cents == null) return '$0'
  const dollars = Math.abs(cents) / 100
  const decimals = opts.decimals != null ? opts.decimals : (dollars >= 1000 ? 0 : 2)
  let str
  if (dollars >= 1000000) str = '$' + (dollars / 1000000).toFixed(1) + 'M'
  else if (dollars >= 10000) str = '$' + (dollars / 1000).toFixed(0) + 'k'
  else str = '$' + dollars.toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals })
  return cents < 0 ? '\u2212' + str : str
}

function layoutSankey(nodes, links, width, height) {
  const padding = { top: 24, right: 24, bottom: 16, left: 24 }
  const cols = [[], [], [], []]
  for (const n of nodes) cols[n.col].push(n)

  const innerW = width - padding.left - padding.right
  const innerH = height - padding.top - padding.bottom
  const nodeW = 14
  const colX = [0, innerW / 3, (innerW / 3) * 2, innerW].map(x => x + padding.left)

  const nodeGapPx = 8
  const colTotals = cols.map(arr => arr.reduce((s, n) => s + n.value_cents, 0))
  const totalMax = Math.max(...colTotals)

  function colScale(col) {
    const nNodes = cols[col].length
    const totalGap = Math.max(0, (nNodes - 1)) * nodeGapPx
    return (innerH - totalGap) / totalMax
  }

  cols.forEach((arr, ci) => {
    arr.sort((a, b) => {
      if (a.kind === 'net') return 1
      if (b.kind === 'net') return -1
      return b.value_cents - a.value_cents
    })
  })

  const nodeMap = {}
  cols.forEach((arr, ci) => {
    const s = colScale(ci)
    let y = padding.top
    for (const n of arr) {
      n.x = colX[ci] - (ci === 3 ? nodeW : 0)
      if (ci === 0) n.x = padding.left
      n.y0 = y
      n.h = Math.max(2, n.value_cents * s)
      n.y1 = y + n.h
      n.w = nodeW
      y = n.y1 + nodeGapPx
      nodeMap[n.id] = n
    }
  })

  // Attach link endpoints
  const outBy = new Map()
  const inBy = new Map()
  for (const l of links) {
    if (!outBy.has(l.source)) outBy.set(l.source, [])
    outBy.get(l.source).push(l)
    if (!inBy.has(l.target)) inBy.set(l.target, [])
    inBy.get(l.target).push(l)
  }

  for (const [nid, arr] of outBy) {
    const src = nodeMap[nid]
    if (!src) continue
    arr.sort((a, b) => {
      const ta = nodeMap[a.target]
      const tb = nodeMap[b.target]
      return (ta ? ta.y0 : 0) - (tb ? tb.y0 : 0)
    })
    const s = colScale(src.col)
    let y = src.y0
    for (const l of arr) {
      const lh = Math.max(1.2, l.value_cents * s)
      l.sy0 = y
      l.sy1 = y + lh
      y += lh
    }
  }

  for (const [nid, arr] of inBy) {
    const tgt = nodeMap[nid]
    if (!tgt) continue
    arr.sort((a, b) => {
      const sa = nodeMap[a.source]
      const sb = nodeMap[b.source]
      return (sa ? sa.y0 : 0) - (sb ? sb.y0 : 0)
    })
    const s = colScale(tgt.col)
    let y = tgt.y0
    for (const l of arr) {
      const lh = Math.max(1.2, l.value_cents * s)
      l.ty0 = y
      l.ty1 = y + lh
      y += lh
    }
  }

  return { cols, nodeW, padding, innerW, innerH, nodeMap }
}

function linkPath(l, nodeMap) {
  const src = nodeMap[l.source]
  const tgt = nodeMap[l.target]
  if (!src || !tgt) return ''
  const x0 = src.x + src.w
  const x1 = tgt.x
  const my = (x0 + x1) / 2
  const sy = (l.sy0 + l.sy1) / 2
  const ty = (l.ty0 + l.ty1) / 2
  const half = (l.sy1 - l.sy0) / 2
  return `M ${x0},${sy - half}
          C ${my},${sy - half} ${my},${ty - half} ${x1},${ty - half}
          L ${x1},${ty + half}
          C ${my},${ty + half} ${my},${sy + half} ${x0},${sy + half} Z`
}

export default function SankeyPanel({ data, height = 460 }) {
  const wrapRef = useRef(null)
  const [size, setSize] = useState({ w: 900, h: height })
  const [hover, setHover] = useState(null)
  const [tooltip, setTooltip] = useState(null)

  useEffect(() => {
    if (!wrapRef.current) return
    const ro = new ResizeObserver(es => {
      for (const e of es) setSize(s => ({ ...s, w: e.contentRect.width }))
    })
    ro.observe(wrapRef.current)
    return () => ro.disconnect()
  }, [])

  const { nodes, links } = data || { nodes: [], links: [] }

  const lay = useMemo(() => {
    if (!nodes.length) return null
    return layoutSankey(nodes, links, size.w, size.h)
  }, [nodes, links, size.w, size.h])

  // Determine which links/nodes are "lit" given hover
  const lit = useMemo(() => {
    if (!lay || !hover) return null
    const nodeMap = lay.nodeMap
    const litNodes = new Set()
    const litLinks = new Set()

    const focusNode = hover
    if (!nodeMap[focusNode]) return null

    const fwd = new Set([focusNode])
    let frontier = [focusNode]
    while (frontier.length) {
      const nxt = []
      for (const l of links) {
        if (frontier.includes(l.source) && !fwd.has(l.target)) {
          fwd.add(l.target)
          nxt.push(l.target)
          litLinks.add(l)
        }
        if (frontier.includes(l.target) && !fwd.has(l.source)) {
          fwd.add(l.source)
          nxt.push(l.source)
          litLinks.add(l)
        }
      }
      frontier = nxt
    }
    for (const n of fwd) litNodes.add(n)
    return { nodes: litNodes, links: litLinks }
  }, [lay, hover, links])

  if (!nodes.length) {
    return (
      <div ref={wrapRef} className="sankey-wrap">
        <div className="sub" style={{ textAlign: 'center', padding: 40 }}>No data for Sankey diagram</div>
      </div>
    )
  }

  return (
    <div ref={wrapRef} className="sankey-wrap">
      <div className="sankey-cols">
        <span>Sources</span>
        <span>Income → Allocation</span>
        <span>Subcategories</span>
      </div>
      <svg className="sankey" viewBox={`0 0 ${size.w} ${size.h}`} height={size.h}>
        {links.map((l, i) => {
          if (!lay) return null
          const src = lay.nodeMap[l.source]
          const tgt = lay.nodeMap[l.target]
          if (!src || !tgt) return null
          const isLit = lit && lit.links.has(l)
          const dim = lit && !isLit
          const color = tgt.kind === 'net' ? '#a3e635' : (tgt.color || src.color)
          const d = linkPath(l, lay.nodeMap)
          if (!d) return null
          return (
            <path key={i}
              d={d}
              className={`sankey-link ${dim ? 'is-dim' : ''} ${isLit ? 'is-hot' : ''}`}
              fill={color}
              fillOpacity={isLit ? 0.65 : dim ? 0.08 : 0.32}
              onMouseEnter={(e) => setTooltip({ x: e.clientX, y: e.clientY, label: `${src.label} → ${tgt.label}`, value: l.value_cents })}
              onMouseMove={(e) => setTooltip(t => t && { ...t, x: e.clientX, y: e.clientY })}
              onMouseLeave={() => setTooltip(null)}
            />
          )
        })}
        {nodes.map(n => {
          if (!lay) return null
          const ln = lay.nodeMap[n.id]
          if (!ln) return null
          const dim = lit && !lit.nodes.has(n.id)
          return (
            <g key={n.id} className="sankey-node"
               onMouseEnter={() => setHover(n.id)}
               onMouseLeave={() => { setHover(null); setTooltip(null) }}>
              <rect x={ln.x} y={ln.y0} width={ln.w} height={ln.h} fill={n.color} opacity={dim ? 0.3 : 1} rx={2}/>
              <text className="sankey-node-label"
                x={n.col === 3 ? ln.x - 6 : ln.x + ln.w + 6}
                y={ln.y0 + Math.min(14, ln.h / 2 + 4)}
                textAnchor={n.col === 3 ? 'end' : 'start'}
                opacity={dim ? 0.35 : 1}>
                {n.label}
              </text>
              {ln.h > 26 && (
                <text className="sankey-node-value"
                  x={n.col === 3 ? ln.x - 6 : ln.x + ln.w + 6}
                  y={ln.y0 + Math.min(28, ln.h / 2 + 18)}
                  textAnchor={n.col === 3 ? 'end' : 'start'}
                  opacity={dim ? 0.35 : 1}>
                  {fmtCurrency(n.value_cents, { compact: true })}
                </text>
              )}
            </g>
          )
        })}
      </svg>
      {tooltip && (
        <div className="tooltip" style={{ left: tooltip.x + 12, top: tooltip.y + 12, position: 'absolute' }}>
          <div className="t-label">{tooltip.label}</div>
          <div className="t-value">{fmtCurrency(tooltip.value)}</div>
        </div>
      )}
    </div>
  )
}
