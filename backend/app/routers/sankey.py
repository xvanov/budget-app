from fastapi import APIRouter, Query
from typing import Optional
from app.database import get_db
from app.services.scope import EFFECTIVE_NET_SUM

router = APIRouter()

CAT_COLORS = [
    "#a78bfa", "#f472b6", "#34d399", "#fbbf24", "#60a5fa",
    "#fb923c", "#94a3b8", "#38bdf8", "#4ade80", "#f43f5e",
    "#22d3ee", "#c084fc", "#f87171", "#2dd4bf", "#facc15",
]

def _hash_color(name):
    h = 0
    for c in name:
        h = (h * 31 + ord(c)) & 0xFFFFFFFF
    return CAT_COLORS[abs(h) % len(CAT_COLORS)]

@router.get("/api/sankey")
async def get_sankey(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    scope: Optional[str] = "auto",
):
    """Where every dollar earned in the period ended up.

    This MUST agree with ``/api/dashboard`` to the cent — it is the same money
    described two ways, shown on the same screen, and a diagram that contradicts
    the KPI cards above it is worse than no diagram. Two things broke that:

    * **No scope filter.** The dashboard reports the live (Plaid) rows; this query
      scanned every row including the superseded CSV history, which duplicates
      years of the same transactions. On the real data that inflated all-time
      income from $247,707 to $1,592,352 — 6.4x — and the Cash Flow panel sat
      directly under KPI cards saying something entirely different.
    * **Gross per category instead of net.** Summing a category's inflows and
      outflows separately puts a category with both on BOTH sides of the diagram,
      while the dashboard lets the category's NET decide its single side. That is
      the whole point of the flat model: a refund reduces its category rather than
      appearing as income (see ``services/flat_categories``).
    """
    from app.services.scope import resolve_scope

    db = await get_db()
    try:
        scope_sql, scope_params, effective_scope, _note = await resolve_scope(db, scope)
        where_clause = scope_sql
        params = list(scope_params)
        if start_date:
            where_clause += " AND t.posted_date >= ?"
            params.append(start_date)
        if end_date:
            where_clause += " AND t.posted_date <= ?"
            params.append(end_date)

        cursor = await db.execute(
            # Effective amounts: a purchase refunded in a later period stops
            # flowing here, and the refund credit never becomes an income node.
            # See services/scope.py → refund attribution.
            #
            # NET per category, exactly as get_dashboard aggregates, so the two
            # cannot drift. Uncategorized is excluded for the same reason it is
            # there: it names no bucket, so it cannot be a node.
            f"""SELECT t.category_path,
                       {EFFECTIVE_NET_SUM} as net
                FROM transactions t
                WHERE (t.entity_type IS NULL OR t.entity_type NOT IN ('transfer', 'asset_conversion', 'excluded'))
                  AND t.category_path IS NOT NULL
                  AND t.category_path != 'Uncategorized'
                  {where_clause}
                GROUP BY t.category_path""",
            params
        )
        rows = await cursor.fetchall()

        # Categorize into income and expense paths
        income_paths = {}  # category_path -> total_cents
        expense_paths = {}
        total_income_cents = 0
        total_expense_cents = 0

        for row in rows:
            path = row["category_path"]
            # The category's NET decides which side it belongs on — one side only.
            net = row["net"] or 0
            if net > 0:
                income_paths[path] = income_paths.get(path, 0) + net
                total_income_cents += net
            elif net < 0:
                expense_paths[path] = expense_paths.get(path, 0) + (-net)
                total_expense_cents += -net

        net_cents = total_income_cents - total_expense_cents

        # Build income node hierarchy:
        #   level 0 (income-sub): leaf (3rd component or whole for 2-level)
        #   level 1 (income-cat): 2nd component
        income_subs = {}   # label -> total
        income_cats = {}   # label -> total
        income_sub_to_cat = {}  # sub label -> cat label

        for path, amt in income_paths.items():
            parts = [p.strip() for p in path.split(">")]
            if len(parts) >= 3:
                cat = parts[1]
                sub = " > ".join(parts[1:])
            else:
                cat = parts[1] if len(parts) > 1 else parts[0]
                sub = cat
            income_subs[sub] = income_subs.get(sub, 0) + amt
            income_cats[cat] = income_cats.get(cat, 0) + amt
            income_sub_to_cat[sub] = cat

        # Build expense node hierarchy:
        #   level 2 (expense-cat): 2nd component
        #   level 3 (expense-sub): leaf (3rd component or whole for 2-level)
        expense_cats = {}
        expense_subs = {}
        expense_sub_to_cat = {}

        for path, amt in expense_paths.items():
            parts = [p.strip() for p in path.split(">")]
            if len(parts) >= 3:
                cat = parts[1]
                sub = " > ".join(parts[1:])
            else:
                cat = parts[1] if len(parts) > 1 else parts[0]
                sub = cat
            expense_cats[cat] = expense_cats.get(cat, 0) + amt
            expense_subs[sub] = expense_subs.get(sub, 0) + amt
            expense_sub_to_cat[sub] = cat

        nodes = []
        links = []
        node_ids = set()

        def add_node(nid, label, value, col, kind):
            if nid in node_ids:
                return
            node_ids.add(nid)
            nodes.append({
                "id": nid,
                "label": label,
                "value_cents": value,
                "color": _hash_color(label),
                "col": col,
                "kind": kind,
            })

        # Col 0: income subs
        for sub, amt in sorted(income_subs.items(), key=lambda x: -x[1]):
            add_node("isub:" + sub, sub, amt, 0, "income-sub")

        # Col 1: income cats
        for cat, amt in sorted(income_cats.items(), key=lambda x: -x[1]):
            add_node("icat:" + cat, cat, amt, 1, "income-cat")

        # Col 2: expense cats + net savings
        for cat, amt in sorted(expense_cats.items(), key=lambda x: -x[1]):
            add_node("ecat:" + cat, cat, amt, 2, "expense-cat")
        if net_cents > 0:
            add_node("net:savings", "Net Cash Flow", net_cents, 2, "net")

        # Col 3: expense subs
        for sub, amt in sorted(expense_subs.items(), key=lambda x: -x[1]):
            add_node("esub:" + sub, sub, amt, 3, "expense-sub")

        # Links: Col 0 → Col 1 (income sub to income cat)
        for sub, cat in income_sub_to_cat.items():
            amt = income_subs.get(sub, 0)
            if amt > 0:
                links.append({
                    "source": "isub:" + sub,
                    "target": "icat:" + cat,
                    "value_cents": amt,
                })

        # Links: Col 1 → Col 2 (proportional allocation)
        if total_income_cents > 0:
            for icat, ival in income_cats.items():
                share = ival / total_income_cents
                for ecat, eval_ in expense_cats.items():
                    link_val = int(eval_ * share)
                    if link_val > 0:
                        links.append({
                            "source": "icat:" + icat,
                            "target": "ecat:" + ecat,
                            "value_cents": link_val,
                        })
                if net_cents > 0:
                    links.append({
                        "source": "icat:" + icat,
                        "target": "net:savings",
                        "value_cents": int(net_cents * share),
                    })

        # Links: Col 2 → Col 3 (expense cat to subs)
        for sub, cat in expense_sub_to_cat.items():
            amt = expense_subs.get(sub, 0)
            if amt > 0:
                links.append({
                    "source": "ecat:" + cat,
                    "target": "esub:" + sub,
                    "value_cents": amt,
                })

        return {
            "nodes": nodes,
            "links": links,
            "total_income_cents": total_income_cents,
            "total_expense_cents": total_expense_cents,
            "net_cents": net_cents,
        }
    finally:
        await db.close()
