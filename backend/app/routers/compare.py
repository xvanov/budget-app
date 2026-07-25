from fastapi import APIRouter, Query
from typing import Optional
from app.database import get_db
from app.services.scope import EFFECTIVE_EXPENSE_SUM, EFFECTIVE_INCOME_SUM

router = APIRouter()

@router.get("/api/compare")
async def get_compare(
    period1_start: str = Query(...),
    period1_end: str = Query(...),
    period2_start: str = Query(...),
    period2_end: str = Query(...),
):
    db = await get_db()
    try:
        periods = [
            ("period1", period1_start, period1_end),
            ("period2", period2_start, period2_end),
        ]
        results = {}
        totals = {}

        for pkey, pstart, pend in periods:
            params = [pstart, pend]
            # Effective amounts, so a period is not blamed for a purchase whose
            # refund arrived in the other one. See services/scope.py.
            cursor = await db.execute(
                f"""SELECT
                    {EFFECTIVE_INCOME_SUM} as income,
                    {EFFECTIVE_EXPENSE_SUM} as expense
                FROM transactions t
                WHERE t.posted_date >= ? AND t.posted_date <= ?
                  AND (t.entity_type IS NULL OR t.entity_type NOT IN ('transfer', 'asset_conversion', 'excluded'))""",
                params
            )
            row = await cursor.fetchone()
            income = row[0] if row else 0
            expense = row[1] if row else 0
            totals[pkey] = {"income_cents": income, "expense_cents": expense, "net_cents": income - expense}

            cursor = await db.execute(
                f"""SELECT t.category_path,
                        {EFFECTIVE_INCOME_SUM} as income,
                        {EFFECTIVE_EXPENSE_SUM} as expense
                FROM transactions t
                WHERE t.posted_date >= ? AND t.posted_date <= ?
                  AND (t.entity_type IS NULL OR t.entity_type NOT IN ('transfer', 'asset_conversion', 'excluded'))
                  AND t.category_path IS NOT NULL
                GROUP BY t.category_path
                ORDER BY expense DESC""",
                params
            )
            by_category = {}
            for row in await cursor.fetchall():
                by_category[row["category_path"]] = {
                    "income_cents": row["income"],
                    "expense_cents": row["expense"],
                }
            results[pkey] = by_category

        all_cats = set(list(results["period1"].keys()) + list(results["period2"].keys()))
        categories = []
        for cat in sorted(all_cats):
            p1 = results["period1"].get(cat, {"income_cents": 0, "expense_cents": 0})
            p2 = results["period2"].get(cat, {"income_cents": 0, "expense_cents": 0})
            income1 = p1["income_cents"]
            expense1 = p1["expense_cents"]
            income2 = p2["income_cents"]
            expense2 = p2["expense_cents"]

            flow = "income" if (income1 > 0 or income2 > 0) else "expense"
            val1 = income1 if flow == "income" else expense1
            val2 = income2 if flow == "income" else expense2
            delta = val1 - val2
            pct = ((delta / val2) * 100) if val2 != 0 else (100 if val1 != 0 else 0)

            categories.append({
                "category_path": cat,
                "flow": flow,
                "period1_cents": val1,
                "period2_cents": val2,
                "delta_cents": delta,
                "delta_pct": round(pct, 1),
            })

        categories.sort(key=lambda c: abs(c["delta_cents"]), reverse=True)

        return {
            "period1": {"start": period1_start, "end": period1_end, "totals": totals["period1"]},
            "period2": {"start": period2_start, "end": period2_end, "totals": totals["period2"]},
            "categories": categories,
        }
    finally:
        await db.close()
